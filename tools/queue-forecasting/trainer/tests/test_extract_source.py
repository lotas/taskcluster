"""The extract path has to reproduce what the SQL path did, not merely run.

Every assertion here is about a difference that would NOT fail if it were wrong:
a filter applied loosely, a window left wide, a column projected in, a JSON
string never parsed. Each of those trains a model on a different population and
reports a number nobody can tell is wrong -- which is why they are tested against
the SQL they mirror rather than against themselves.

The fixture writes the extractor's real Arrow types (`host/extractor/parquet_writer.py`):
`timestamp('us', tz='UTC')`, `date32`, and `tags` as a JSON *string*.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from datetime import timezone

import pandas as pd
import pyarrow
import pyarrow.parquet
import pytest

from src import data_loader as dl
from src import extract_source as xs
from src.config import Config

UTC = timezone.utc


def ts(day, hour=0, minute=0):
    return datetime.datetime(2026, 4, day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fixture ----------------------------------------------------------------
_RUNS_SCHEMA = pyarrow.schema([
    ("task_id", pyarrow.string()),
    ("run_id", pyarrow.int32()),
    ("pending_at", pyarrow.timestamp("us", tz="UTC")),
    ("started_at", pyarrow.timestamp("us", tz="UTC")),
    ("resolved_at", pyarrow.timestamp("us", tz="UTC")),
    ("reason_resolved", pyarrow.string()),
    ("wait_duration_s", pyarrow.float64()),
    ("run_duration_s", pyarrow.float64()),
    ("priority_at_pending", pyarrow.string()),
    ("queue_pending", pyarrow.int32()),
    ("task_queue_id", pyarrow.string()),
    ("scheduler_id", pyarrow.string()),
    ("metadata_name", pyarrow.string()),
    ("normalized_name", pyarrow.string()),
    ("max_run_time_s", pyarrow.int32()),
    ("repo_family", pyarrow.string()),
    ("tags", pyarrow.string()),
])

# `task_created` is carried by `DATASETS["qctx_runs"]` since 2026-08-30, because
# the tasks-side join floor cannot be re-applied without it.
# `test_qctx_is_refused_when_the_extract_lacks_task_created` covers extracts
# published before that -- the column list is not part of `request_hash`, so an
# older artifact can still be handed back for a matching request.
_QCTX_SCHEMA = pyarrow.schema([
    ("task_id", pyarrow.string()),
    ("run_id", pyarrow.int32()),
    ("pending_at", pyarrow.timestamp("us", tz="UTC")),
    ("started_at", pyarrow.timestamp("us", tz="UTC")),
    ("resolved_at", pyarrow.timestamp("us", tz="UTC")),
    ("priority_at_pending", pyarrow.string()),
    ("task_queue_id", pyarrow.string()),
    ("repo_family", pyarrow.string()),
    ("task_created", pyarrow.timestamp("us", tz="UTC")),
])

_WORKER_COUNTS_SCHEMA = pyarrow.schema([
    ("task_queue_id", pyarrow.string()),
    ("sampled_at", pyarrow.timestamp("us", tz="UTC")),
    ("running_workers", pyarrow.int32()),
    ("claimed_tasks", pyarrow.int32()),
    ("existing_capacity", pyarrow.int32()),
])

_WORKER_POOLS_SCHEMA = pyarrow.schema([
    ("task_queue_id", pyarrow.string()),
    ("pool_kind", pyarrow.string()),
    ("provider_type", pyarrow.string()),
])

_THROUGHPUT_SCHEMA = pyarrow.schema([
    ("task_queue_id", pyarrow.string()),
    ("started_at", pyarrow.timestamp("us", tz="UTC")),
    ("resolved_at", pyarrow.timestamp("us", tz="UTC")),
    ("wait_duration_s", pyarrow.float64()),
    ("run_duration_s", pyarrow.float64()),
])

_HEALTH_FLAGS = [
    "flag_exception_spike", "flag_stuck_pending_spike", "flag_wait_p99_spike",
    "flag_volume_anomaly", "flag_low_completion", "flag_capacity_drop",
    "flag_capacity_spike", "flag_low_utilization", "flag_sampler_offline",
]
_HEALTH_SCHEMA = pyarrow.schema(
    [("sample_date", pyarrow.date32()), ("is_anomalous", pyarrow.bool_())]
    + [(f, pyarrow.bool_()) for f in _HEALTH_FLAGS]
)


def _run_row(day, hour, *, task, wait, started=True, resolved="completed",
             queue_pending=7, kind="build"):
    pending = ts(day, hour)
    return {
        "task_id": task,
        "run_id": 0,
        "pending_at": pending,
        "started_at": pending + datetime.timedelta(seconds=wait or 0)
        if started else None,
        "resolved_at": pending + datetime.timedelta(seconds=(wait or 0) + 60)
        if resolved else None,
        "reason_resolved": resolved,
        "wait_duration_s": wait,
        "run_duration_s": 60.0,
        "priority_at_pending": "normal",
        "queue_pending": queue_pending,
        "task_queue_id": "gecko-1/b-linux",
        "scheduler_id": "gecko-level-1",
        "metadata_name": task,
        "normalized_name": task,
        "max_run_time_s": 3600,
        "repo_family": "gecko",
        "tags": json.dumps({"kind": kind, "worker-implementation": "d-w"}),
    }


def write_extract(root, *, runs_rows=None, window=None, health_rows=None,
                  qctx_rows=None, worker_counts_rows=None,
                  throughput_rows=None):
    """A minimal but schema-faithful extract, manifest included."""
    root.mkdir(parents=True, exist_ok=True)
    window = window or {
        "train_start": ts(1),
        "as_of_date": ts(20),
        "window_lower": ts(1) - datetime.timedelta(days=1),
        "ref_lower": ts(1) - datetime.timedelta(days=15),
    }

    runs_rows = runs_rows if runs_rows is not None else [
        _run_row(5, 1, task="t-a", wait=30.0),
        _run_row(6, 2, task="t-b", wait=300.0),
        _run_row(7, 3, task="t-c", wait=None, started=False, resolved="exception"),
        _run_row(8, 4, task="t-d", wait=-1.0),
        _run_row(9, 5, task="t-e", wait=90.0, queue_pending=None),
        _run_row(19, 6, task="t-f", wait=120.0),
    ]
    health_rows = health_rows if health_rows is not None else [
        {"sample_date": datetime.date(2026, 4, 6), "is_anomalous": True,
         "flag_wait_p99_spike": True},
        {"sample_date": datetime.date(2026, 4, 7), "is_anomalous": True,
         "flag_volume_anomaly": True},
        {"sample_date": datetime.date(2026, 4, 8), "is_anomalous": False},
    ]
    qctx_rows = qctx_rows if qctx_rows is not None else [
        {"task_id": "q-a", "run_id": 0, "pending_at": ts(4, 12),
         "started_at": ts(4, 13), "resolved_at": ts(4, 14),
         "priority_at_pending": "normal", "task_queue_id": "gecko-1/b-linux",
         "repo_family": "gecko", "task_created": ts(4, 11)},
        {"task_id": "q-open", "run_id": 0, "pending_at": ts(4, 12),
         "started_at": None, "resolved_at": None,
         "priority_at_pending": "low", "task_queue_id": "gecko-1/b-linux",
         "repo_family": "gecko", "task_created": ts(4, 11)},
        {"task_id": "q-noqueue", "run_id": 0, "pending_at": ts(5, 12),
         "started_at": None, "resolved_at": None,
         "priority_at_pending": "low", "task_queue_id": None,
         "repo_family": "gecko", "task_created": ts(5, 11)},
        # Pended inside the window, but its TASK predates the reference floor:
        # the SQL drops it via `t.task_created >= ref_lower` and so must this.
        {"task_id": "q-oldtask", "run_id": 0, "pending_at": ts(4, 12),
         "started_at": None, "resolved_at": None,
         "priority_at_pending": "low", "task_queue_id": "gecko-1/b-linux",
         "repo_family": "gecko", "task_created": ts(1) - datetime.timedelta(days=40)},
    ]
    worker_counts_rows = worker_counts_rows if worker_counts_rows is not None else [
        {"task_queue_id": "gecko-1/b-linux", "sampled_at": ts(d, h),
         "running_workers": 3, "claimed_tasks": 2, "existing_capacity": 5}
        for d, h in [(1, 0), (5, 0), (10, 0), (19, 23)]
    ]
    throughput_rows = throughput_rows if throughput_rows is not None else [
        {"task_queue_id": "gecko-1/b-linux", "started_at": ts(d, h),
         "resolved_at": ts(d, h) + datetime.timedelta(minutes=5),
         "wait_duration_s": 30.0, "run_duration_s": 300.0}
        for d, h in [(2, 0), (6, 0), (12, 0)]
    ]

    datasets = {
        "runs": ("runs.parquet", _RUNS_SCHEMA, runs_rows,
                 {"train_start": window["train_start"],
                  "as_of_date": window["as_of_date"]}),
        "worker_counts": ("worker_counts.parquet", _WORKER_COUNTS_SCHEMA,
                          worker_counts_rows,
                          {"window_lower": window["window_lower"],
                           "as_of_date": window["as_of_date"]}),
        "worker_pools": ("worker_pools.parquet", _WORKER_POOLS_SCHEMA,
                         [{"task_queue_id": "gecko-1/b-linux",
                           "pool_kind": "worker-pool",
                           "provider_type": "fxci-level1-gcp"}], {}),
        "throughput_runs": ("throughput_runs.parquet", _THROUGHPUT_SCHEMA,
                            throughput_rows,
                            {"window_lower": window["window_lower"],
                             "as_of_date": window["as_of_date"]}),
        "qctx_runs": ("qctx_runs.parquet", _QCTX_SCHEMA, qctx_rows,
                      {"as_of_date": window["as_of_date"],
                       "ref_lower": window["ref_lower"],
                       "window_lower": window["window_lower"]}),
        "daily_health": ("daily_health.parquet", _HEALTH_SCHEMA, health_rows,
                         {}),
    }

    files = {}
    for name, (filename, schema, rows, bindings) in datasets.items():
        columns = [f.name for f in schema]
        data = {col: [r.get(col) for r in rows] for col in columns}
        table = pyarrow.table(data, schema=schema)
        path = root / filename
        pyarrow.parquet.write_table(table, path)
        files[name] = {
            "file": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": len(rows),
            "window": {k: iso(v) for k, v in bindings.items()},
            "columns": columns,
        }

    manifest = {
        "schema": 1,
        "request": {"target": "wait_time", "lookback_days": 14},
        "request_hash": "r" * 64,
        "files": files,
        "watermark": {"runs": {"pending_at": iso(window["as_of_date"])}},
        "extract_hash": "e" * 64,
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return root


@pytest.fixture(autouse=True)
def _clean_source_state(monkeypatch):
    monkeypatch.delenv(xs.ENV_DIR, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    xs._reset_for_tests()
    yield
    xs._reset_for_tests()


@pytest.fixture
def extract(tmp_path):
    return xs.ExtractSource(write_extract(tmp_path / "extract"))


def _cfg(**overrides):
    base = dict(
        target="wait_time",
        target_column="wait_duration_s",
        # 13 so `compute_windows` puts train_start exactly on the fixture
        # extract's lower bound (as_of - holdout - validation - lookback =
        # Apr 1). A wider config is what `test_runs_refuses_a_window_wider_than
        # _the_extract` covers.
        lookback_days=13,
        holdout_days=5,
        validation_days=1,
        as_of_date=ts(20),
        filters=["r.started_at IS NOT NULL",
                 "r.queue_pending IS NOT NULL",
                 "r.wait_duration_s IS NOT NULL",
                 "r.wait_duration_s >= 0"],
        categorical_features=["task_queue_id", "tags.kind"],
        numeric_features=["queue_pending"],
        derived_features={},
        model_type="lightgbm",
        quantiles=[0.5, 0.9],
        model_params={},
    )
    base.update(overrides)
    return Config(**base)


# --- predicate translation --------------------------------------------------
def _frame():
    return pd.DataFrame({
        "wait_duration_s": [30.0, None, -1.0, 0.0],
        "reason_resolved": ["completed", "failed", "exception", None],
        "queue_pending": [1.0, None, 3.0, 4.0],
    })


@pytest.mark.parametrize("predicate,expected", [
    # Exactly the predicates the promoted configs use.
    ("r.wait_duration_s IS NOT NULL", [0, 2, 3]),
    ("r.wait_duration_s IS NULL", [1]),
    ("r.wait_duration_s >= 0", [0, 3]),
    ("r.queue_pending IS NOT NULL", [0, 2, 3]),
    ("r.reason_resolved IN ('completed', 'failed')", [0, 1]),
    ("(r.wait_duration_s IS NULL OR r.wait_duration_s >= 0)", [0, 1, 3]),
    # Shapes the grammar covers but no config uses yet.
    ("wait_duration_s > 0", [0]),
    ("r.reason_resolved = 'completed'", [0]),
    ("r.reason_resolved != 'completed'", [1, 2]),
    ("r.reason_resolved NOT IN ('completed')", [1, 2]),
    ("r.wait_duration_s >= 0 AND r.reason_resolved = 'completed'", [0]),
])
def test_predicate_matches_sql_semantics(predicate, expected):
    got = xs.apply_filters(_frame(), [predicate])
    assert list(got.index) == expected


def test_comparison_excludes_nulls_as_sql_does():
    """`wait_duration_s >= 0` in SQL drops NULL rows: NULL >= 0 is UNKNOWN."""
    got = xs.apply_filters(_frame(), ["r.wait_duration_s >= 0"])
    assert got["wait_duration_s"].isna().sum() == 0


def test_not_in_excludes_nulls_as_sql_does():
    """`col NOT IN (..)` is UNKNOWN for NULL, so the row is dropped -- pandas'
    `~isin` alone would keep it."""
    got = xs.apply_filters(_frame(), ["r.reason_resolved NOT IN ('completed')"])
    assert got["reason_resolved"].isna().sum() == 0


def test_multiple_filters_are_conjunctive():
    got = xs.apply_filters(
        _frame(), ["r.wait_duration_s IS NOT NULL", "r.wait_duration_s >= 0"])
    assert list(got.index) == [0, 3]


def test_filter_columns_ignores_string_literals():
    assert xs.filter_columns(
        ["r.reason_resolved IN ('completed', 'queue_pending')"]
    ) == {"reason_resolved"}


@pytest.mark.parametrize("predicate", [
    "r.wait_duration_s BETWEEN 0 AND 5",
    "r.metadata_name LIKE 'build%'",
    "EXISTS (SELECT 1)",
    "r.wait_duration_s >= r.run_duration_s",
    "r.a = 1 AND r.b = 2 OR r.c = 3",
    "(r.a = 1",
])
def test_unsupported_predicate_is_refused_not_approximated(predicate):
    with pytest.raises(xs.ExtractError):
        xs.apply_filters(_frame(), [predicate])


def test_filter_on_absent_column_is_refused(extract):
    with pytest.raises(xs.ExtractError, match="does not carry"):
        extract.runs(train_start=ts(1), as_of_date=ts(20),
                     target_column="wait_duration_s",
                     keep_columns=list(dl.BASE_META_COLUMNS),
                     filters=["r.no_such_column IS NOT NULL"])


# --- runs() -----------------------------------------------------------------
def test_runs_applies_window_filters_rename_and_projection(extract):
    c = _cfg()
    got = extract.runs(
        train_start=ts(5), as_of_date=ts(10),
        target_column=c.target_column,
        keep_columns=dl.BASE_META_COLUMNS + sorted(dl._needed_source_columns(c)),
        filters=c.filters,
    )
    # Window: t-f (day 19) excluded. Filters: t-c (null wait, never started),
    # t-d (negative wait), t-e (null queue_pending) excluded.
    assert sorted(got["task_id"]) == ["t-a", "t-b"]
    # The target is renamed, as `_build_query`'s `AS y` does.
    assert "y" in got.columns and "wait_duration_s" not in got.columns
    assert sorted(got["y"]) == [30.0, 300.0]
    # `started_at` is filtered ON but not selected -- the SQL path's SELECT
    # never lists it, so carrying it here would be a column one source has and
    # the other does not.
    assert "started_at" not in got.columns
    # The other target column is not carried either, though the extract has it.
    assert "run_duration_s" not in got.columns
    assert set(got.columns) == set(
        dl.BASE_META_COLUMNS + ["y", "task_queue_id", "queue_pending", "tags"])


def test_runs_parses_tags_to_dict(extract):
    """The extract writes `tags` as JSON text; `FeatureBuilder._extract_tags`
    returns None for anything that is not a dict, silently."""
    got = extract.runs(train_start=ts(1), as_of_date=ts(20),
                       target_column="wait_duration_s",
                       keep_columns=dl.BASE_META_COLUMNS + ["tags"],
                       filters=[])
    assert all(isinstance(t, dict) for t in got["tags"])
    assert got["tags"].iloc[0]["kind"] == "build"


def test_runs_upper_bound_is_exclusive(extract):
    """`_build_query` uses `pending_at < as_of_date`."""
    got = extract.runs(train_start=ts(1), as_of_date=ts(19),
                       target_column="wait_duration_s",
                       keep_columns=list(dl.BASE_META_COLUMNS), filters=[])
    assert "t-f" not in set(got["task_id"])


def test_runs_refuses_a_column_the_extract_lacks(extract):
    with pytest.raises(xs.ExtractError, match="does not carry"):
        extract.runs(train_start=ts(1), as_of_date=ts(20),
                     target_column="wait_duration_s",
                     keep_columns=dl.BASE_META_COLUMNS + ["invented_feature"],
                     filters=[])


# --- containment ------------------------------------------------------------
def test_runs_refuses_a_window_wider_than_the_extract(extract):
    with pytest.raises(xs.ExtractError, match="silently train on a subset"):
        extract.runs(train_start=ts(1) - datetime.timedelta(days=1),
                     as_of_date=ts(20), target_column="wait_duration_s",
                     keep_columns=list(dl.BASE_META_COLUMNS), filters=[])


def test_runs_refuses_an_as_of_beyond_the_extract(extract):
    with pytest.raises(xs.ExtractError, match="silently train on a subset"):
        extract.runs(train_start=ts(1), as_of_date=ts(21),
                     target_column="wait_duration_s",
                     keep_columns=list(dl.BASE_META_COLUMNS), filters=[])


def test_worker_counts_refuses_a_window_wider_than_the_extract(extract):
    with pytest.raises(xs.ExtractError, match="silently train on a subset"):
        extract.worker_counts(ts(1) - datetime.timedelta(days=2), ts(20))


def test_throughput_refuses_a_window_wider_than_the_extract(extract):
    with pytest.raises(xs.ExtractError, match="silently train on a subset"):
        extract.throughput_runs(ts(1) - datetime.timedelta(days=2), ts(20))


def test_qctx_refuses_a_reference_floor_below_the_extract(extract):
    with pytest.raises(xs.ExtractError, match="silently train on a subset"):
        extract.qctx_runs(ts(1), ts(20), ts(1) - datetime.timedelta(days=30))


# --- the other four datasets ------------------------------------------------
def test_worker_counts_bounds_and_ordering(extract):
    got = extract.worker_counts(ts(1), ts(19, 23))
    # Upper bound exclusive, matching `load_worker_counts`.
    assert list(got["sampled_at"]) == [ts(1), ts(5), ts(10)]
    assert got["sampled_at"].is_monotonic_increasing


def test_throughput_upper_bound_is_inclusive(extract):
    """`load_task_runs_for_throughput` uses `resolved_at <= window_end`."""
    boundary = ts(12) + datetime.timedelta(minutes=5)
    got = extract.throughput_runs(ts(1), boundary)
    assert boundary in set(got["resolved_at"])


def test_qctx_keeps_open_runs_and_drops_exited_ones(extract):
    """`exit IS NULL OR exit > window_start`, exit = COALESCE(started, resolved)."""
    got = extract.qctx_runs(ts(5), ts(20), ts(1) - datetime.timedelta(days=15))
    ids = set(got["task_id"])
    assert "q-open" in ids       # never exited
    assert "q-a" not in ids      # started on day 4, before window_start
    assert "q-noqueue" not in ids  # task_queue_id IS NOT NULL in the SQL
    assert "q-oldtask" not in ids  # t.task_created >= ref_lower
    # `task_created` is filtered on and then dropped: the DB path never selects
    # it, and a column the feature code sees on one source only is a difference
    # between sources that nothing would report.
    assert "task_created" not in got.columns


def test_qctx_is_refused_when_the_extract_lacks_task_created(tmp_path):
    """The shape of any extract published before 2026-08-30.

    Without the column the tasks-side floor cannot be re-applied and the
    reference set would be a superset of the SQL cohort's -- so the read is
    refused, not approximated. `request_hash` does not cover the column list, so
    an older artifact is still a D20 hit for a matching request and this is the
    error that says so; the operator's fix is a bumped `generation`.
    """
    root = write_extract(tmp_path / "extract")
    manifest = json.loads((root / "MANIFEST.json").read_text())
    manifest["files"]["qctx_runs"]["columns"] = [
        c for c in manifest["files"]["qctx_runs"]["columns"] if c != "task_created"]
    (root / "MANIFEST.json").write_text(json.dumps(manifest))

    src = xs.ExtractSource(root)
    with pytest.raises(xs.ExtractError, match="task_created"):
        src.qctx_runs(ts(5), ts(20), ts(1) - datetime.timedelta(days=15))


def test_worker_pools_is_whole_table(extract):
    assert list(extract.worker_pools()["provider_type"]) == ["fxci-level1-gcp"]


def test_anomalous_dates_default_and_subset(extract):
    assert extract.anomalous_dates(None) == {
        datetime.date(2026, 4, 6), datetime.date(2026, 4, 7)}
    assert extract.anomalous_dates(["flag_wait_p99_spike"]) == {
        datetime.date(2026, 4, 6)}
    assert extract.anomalous_dates(["flag_capacity_drop"]) == set()


def test_anomalous_dates_returns_date_objects(extract):
    """The DB path returns `datetime.date`, and the caller compares against
    `pending_at.dt.date`. A Timestamp here would never match, and the anomaly
    filter would silently exclude nothing."""
    for value in extract.anomalous_dates(None):
        assert type(value) is datetime.date


# --- integrity --------------------------------------------------------------
def test_missing_manifest_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(xs.ExtractError, match="MANIFEST"):
        xs.ExtractSource(tmp_path / "empty")


def test_mutated_file_is_refused(tmp_path):
    root = write_extract(tmp_path / "extract")
    src = xs.ExtractSource(root)
    (root / "worker_pools.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(xs.ExtractError, match="does not match the digest"):
        src.worker_pools()


def test_verification_can_be_disabled_explicitly(tmp_path, monkeypatch):
    root = write_extract(tmp_path / "extract")
    monkeypatch.setenv(xs.ENV_VERIFY, "0")
    src = xs.ExtractSource(root)
    assert len(src.worker_pools()) == 1


def test_lineage_names_the_data(extract):
    lineage = extract.lineage()
    assert lineage["extract_hash"] == "e" * 64
    assert set(lineage["files"]) == {
        "runs", "worker_counts", "worker_pools", "throughput_runs",
        "qctx_runs", "daily_health"}
    assert all(f["sha256"] for f in lineage["files"].values())


# --- selection --------------------------------------------------------------
def test_active_is_none_without_configuration():
    assert xs.active() is None


def test_env_var_selects_an_extract(tmp_path, monkeypatch):
    root = write_extract(tmp_path / "extract")
    monkeypatch.setenv(xs.ENV_DIR, str(root))
    assert xs.active().root == root.resolve()


def test_configure_overrides_the_env(tmp_path, monkeypatch):
    a = write_extract(tmp_path / "a")
    b = write_extract(tmp_path / "b")
    monkeypatch.setenv(xs.ENV_DIR, str(a))
    xs.configure(b)
    assert xs.active().root == b.resolve()


# --- data_loader wiring -----------------------------------------------------
def test_loaders_take_the_extract_path_without_a_database_url(tmp_path):
    """DATABASE_URL is unset here (autouse fixture). Under the sandbox it is
    absent for the same reason, and a loader that reached for it would raise
    KeyError before training started."""
    root = write_extract(tmp_path / "extract")
    xs.configure(root)
    c = _cfg(anomaly_filter={"enabled": True,
                             "flag_subset": ["flag_wait_p99_spike"]})

    df = dl.load(c)
    assert sorted(df["task_id"]) == ["t-a", "t-b", "t-f"]
    assert "y" in df.columns
    assert dl.load_anomalous_dates(c) == {datetime.date(2026, 4, 6)}
    assert len(dl.load_worker_pools()) == 1
    assert len(dl.load_worker_counts(c, ts(1), ts(19, 23))) == 3
    assert len(dl.load_task_runs_for_throughput(c, ts(1), ts(20))) == 3


def test_every_reference_window_fits_inside_a_real_extract_window(tmp_path):
    """The containment checks have to pass for a cohort whose `train_start`
    equals the extract's -- which is the normal case, and the one where an
    off-by-a-prefix bound would refuse every legitimate run.

    The fixture derives `window_lower`/`ref_lower` the way `extract_spec` does
    (train_start - 24h, then minus `lookback_days`), so this exercises the real
    arithmetic against the trainer's three reference windows at once:
    throughput reaches back `max(windows)+30m`, velocity `120m`, and
    queue-context `90m + lookback_days`.
    """
    xs.configure(write_extract(tmp_path / "extract"))
    c = _cfg(
        # `add_queue_context_features` reads `priority_at_pending` and
        # `repo_family` off the main frame, and the loader only selects a column
        # a config declares -- which is why every promoted qctx config declares
        # them. Same requirement on both sources.
        categorical_features=["task_queue_id", "priority_at_pending",
                              "repo_family", "tags.kind"],
        throughput_features={"enabled": True, "windows_minutes": [15, 60]},
        velocity_features={"enabled": True, "trailing_windows_minutes": [60],
                           "tolerance_minutes": 10},
        queue_context_features={"enabled": True, "version": 1},
    )
    df = dl.load(c)
    assert len(df) > 0
    # Each of the three reference loaders contributed its columns rather than
    # refusing the window.
    assert "running_workers_now" in df.columns          # velocity
    assert "queue_tasks_completed_60m" in df.columns    # throughput
    from src.queue_context import FEATURE_COLUMNS
    assert set(FEATURE_COLUMNS) <= set(df.columns)       # queue context


def test_extract_path_writes_no_cache(tmp_path, monkeypatch):
    """Two sources sharing one cache filename would be indistinguishable
    afterwards; the extract path therefore writes nothing there."""
    monkeypatch.setattr(dl, "CACHE_DIR", tmp_path / "cache")
    xs.configure(write_extract(tmp_path / "extract"))
    dl.load(_cfg())
    assert not (tmp_path / "cache").exists()


def test_extract_path_ignores_a_stale_cache(tmp_path, monkeypatch):
    """A cache file left by an earlier DB run must not be preferred over the
    extract the caller asked for."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(dl, "CACHE_DIR", cache)
    c = _cfg()
    pd.DataFrame({"task_id": ["stale"], "run_id": [0], "y": [1.0]}) \
        .to_parquet(dl.cache_path(c), index=False)
    xs.configure(write_extract(tmp_path / "extract"))
    assert "stale" not in set(dl.load(c)["task_id"])


def test_needed_source_columns_matches_the_sql_select():
    """The two sources derive their column set from one function, so a config
    cannot get a feature on one path and not the other."""
    c = _cfg(numeric_features=["queue_pending", "max_run_time_s"],
             categorical_features=["scheduler_id", "tags.kind"])
    needed = dl._needed_source_columns(c)
    assert needed == {"queue_pending", "max_run_time_s", "scheduler_id", "tags"}
    sql = dl._build_query(c)
    for col in needed:
        assert f".{col}" in sql


def test_load_is_row_order_canonical_whatever_the_source_order(tmp_path):
    """Order is not incidental: LightGBM samples rows BY INDEX to build bin
    boundaries, so the same rows in a different order train a different model.

    `_build_query` has no ORDER BY, so the database side has no order to
    promise -- which is why the sort lives in `load()`, applied to both sources,
    rather than in the extract adapter.
    """
    rows = [
        _run_row(9, 5, task="t-late", wait=90.0),
        _run_row(5, 1, task="t-early", wait=30.0),
        _run_row(6, 2, task="t-mid", wait=300.0),
    ]
    xs.configure(write_extract(tmp_path / "a", runs_rows=rows))
    first = dl.load(_cfg())
    xs._reset_for_tests()
    xs.configure(write_extract(tmp_path / "b", runs_rows=list(reversed(rows))))
    second = dl.load(_cfg())

    assert list(first["task_id"]) == ["t-early", "t-mid", "t-late"]
    assert list(first["task_id"]) == list(second["task_id"])
    assert first["pending_at"].is_monotonic_increasing


def test_canonical_sort_breaks_ties_on_a_total_key(tmp_path):
    """Same `pending_at` on every row: without task_id/run_id in the key the
    result would keep whatever order arrived."""
    rows = [
        _run_row(5, 1, task="t-c", wait=30.0),
        _run_row(5, 1, task="t-a", wait=40.0),
        _run_row(5, 1, task="t-b", wait=50.0),
    ]
    xs.configure(write_extract(tmp_path / "extract", runs_rows=rows))
    got = dl.load(_cfg())
    assert list(got["task_id"]) == ["t-a", "t-b", "t-c"]
