"""The six query shapes, as data. Phase 2b-1 Task 2.

WHAT THIS MODULE EXISTS TO MAKE TRUE (design D4, plan D18). The trainer's loader
builds its SQL from a config that lives in `qf-research`:

    f"r.{c.target_column} AS y"                   # data_loader.py:199
    where = [..., *c.filters]                     # data_loader.py:240
    query = f"SELECT sample_date ... WHERE {condition}"   # data_loader.py:527

Each of those is fine there and forbidden here. Trusted code that executed a
column name or a predicate chosen by the research repo would defeat the claim
that a new table or column needs a human promotion -- and would defeat it
silently, which is worse than defeating it loudly.

So every query below is a **module constant**. The only thing that varies is a
bound parameter, and the parameter names are declared alongside the SQL so a
test can require the two to agree in both directions.

STDLIB ONLY, deliberately. `extractor.py` needs `pyarrow` and `psycopg`; this
module needs neither, so the D4 regression tests run anywhere the dispatcher's
own tests run -- including on a machine with no database and no extractor
environment.

THE INVENTORY IS A SUPERSET, NOT A SELECTION. Each query returns every column
the union of the trainer's six queries can select, unfiltered. A candidate that
wants a different derivation needs no trusted change; a candidate that wants a
column not named here cannot have one without a human editing this file. That is
the whole line D4 draws, and it is drawn here in about a hundred lines of
literal SQL.
"""
from __future__ import annotations

# NO WINDOW ARITHMETIC AND NO LOOKBACK CONSTANT HERE, deliberately.
#
# Every derived bound -- `window_lower`, `ref_lower` -- is computed by
# `extract_spec.validate` and carried in the validated request, for two reasons:
#
#   1. One place. A constant duplicated across the dispatcher and the extractor
#      is a constant that will be updated in one of them.
#   2. It lands in `request_hash`. A bound this module derived privately would
#      not be in the hashed record, so two extracts with the same `request_hash`
#      could have queried different windows -- which would make D20's immutable
#      reuse quietly wrong.
#
# So `bindings()` is a lookup with no arithmetic in it at all.

class Dataset:
    """One extract file: its SQL, its bound parameters, and what it produces.

    `columns` is declared rather than discovered from the cursor because the
    manifest describes the file, and a description that is read back out of the
    thing it describes cannot detect a change in it.
    """

    __slots__ = ("file", "sql", "params", "columns", "watermark_columns")

    def __init__(self, file, sql, params, columns, watermark_columns):
        self.file = file
        self.sql = sql
        self.params = tuple(params)
        self.columns = tuple(columns)
        self.watermark_columns = tuple(watermark_columns)


# --- runs -------------------------------------------------------------------
#
# The union of what `_build_query` can select, with two deliberate differences:
#
#   * BOTH duration columns, under their own names, and no `AS y`. The target is
#     a candidate-side rename, so no query varies with it -- which is also what
#     lets `request_hash` cover the target without any query depending on it.
#   * `started_at` is selected, which `_build_query` omits. The queue-context
#     loader selects it and bet 2's censoring filters on it, and the union rule
#     says the widest superset wins.
#
# The upper bound is EXCLUSIVE, matching `_build_query`'s `pending_at < as_of`.
_RUNS_SQL = """
SELECT r.task_id,
       r.run_id,
       r.pending_at,
       r.started_at,
       r.resolved_at,
       r.reason_resolved,
       r.wait_duration_s,
       r.run_duration_s,
       r.priority_at_pending,
       r.queue_pending,
       t.task_queue_id,
       t.scheduler_id,
       t.metadata_name,
       t.normalized_name,
       t.max_run_time_s,
       t.repo_family,
       t.tags
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.pending_at >= %(train_start)s
  AND r.pending_at < %(as_of_date)s
"""

# --- worker_counts ----------------------------------------------------------
_WORKER_COUNTS_SQL = """
SELECT task_queue_id,
       sampled_at,
       running_workers,
       claimed_tasks,
       existing_capacity
FROM queue_forecast_worker_counts
WHERE sampled_at >= %(window_lower)s
  AND sampled_at < %(as_of_date)s
"""

# --- worker_pools -----------------------------------------------------------
# Whole table, ~650 rows. No window: a dimension table filtered by a time range
# would silently drop pools that stopped being sampled inside the window, and
# `pool_kind` / `provider_type` are exactly what a candidate joins on.
_WORKER_POOLS_SQL = """
SELECT task_queue_id,
       pool_kind,
       provider_type
FROM queue_forecast_worker_pools
"""

# --- throughput_runs --------------------------------------------------------
# The upper bound is INCLUSIVE, matching `load_task_runs_for_throughput`'s
# `resolved_at <= window_end`. Copying each of the trainer's bounds faithfully
# matters more than making the six consistent with each other: an exclusive
# bound here would drop rows resolving exactly at the boundary, and `as_of_date`
# is midnight, where a boundary row is unremarkable.
_THROUGHPUT_SQL = """
SELECT t.task_queue_id,
       r.started_at,
       r.resolved_at,
       r.wait_duration_s,
       r.run_duration_s
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.resolved_at IS NOT NULL
  AND r.resolved_at >= %(window_lower)s
  AND r.resolved_at <= %(as_of_date)s
  AND t.task_queue_id IS NOT NULL
"""

# --- qctx_runs --------------------------------------------------------------
# A reference run is relevant to a training row at time T iff it was pending at
# T. This is a superset of what any row in the window can see; the leakage-safe
# per-target filtering happens in the candidate.
#
# BOTH SIDES ARE FLOORED at `ref_lower`, and that is load-bearing rather than an
# optimisation: the loader's docstring records that without the tasks-side floor
# this join scans the full history of an ever-growing table, confirmed by
# EXPLAIN as a multi-TB read profile.
#
# BOTH BOUNDS HANG OFF `window_lower`, NOT `train_start`, and that distinction
# was a real subset bug. The trainer calls this query as
# `load_task_runs_for_queue_context(c, w.train_start - 90m, ..)` and derives its
# reference floor from that shifted start. Using `train_start` here made the
# floor 90 minutes late AND made the overlap predicate drop any run that exited
# between `train_start - 90m` and `train_start` -- reference runs that affect
# queue-context features for the first rows of the window. Neither would have
# failed anything: the extract would simply have been a subset, and the model
# trained on it slightly different.
_QCTX_SQL = """
SELECT r.task_id,
       r.run_id,
       r.pending_at,
       r.started_at,
       r.resolved_at,
       r.priority_at_pending,
       t.task_queue_id,
       t.repo_family
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.pending_at < %(as_of_date)s
  AND r.pending_at >= %(ref_lower)s
  AND (COALESCE(r.started_at, r.resolved_at) IS NULL
       OR COALESCE(r.started_at, r.resolved_at) > %(window_lower)s)
  AND t.task_queue_id IS NOT NULL
  AND t.task_created >= %(ref_lower)s
"""

# --- daily_health -----------------------------------------------------------
# THE WHOLE ROW SET, every flag, no predicate.
#
# `load_anomalous_dates` builds `WHERE {condition}` from a config's
# `flag_subset` and returns a set of dates. The values are allowlisted so it is
# not injectable today, but it is a config-driven SQL fragment in trusted code
# and that is precisely the shape D4 forbids. Emitting the rows instead deletes
# the f-string rather than making it safe, and moves the filter to the candidate
# where it belongs: a set of dates is the RESULT of a config-dependent filter,
# and the rows are the fact.
_DAILY_HEALTH_SQL = """
SELECT sample_date,
       is_anomalous,
       flag_exception_spike,
       flag_stuck_pending_spike,
       flag_wait_p99_spike,
       flag_volume_anomaly,
       flag_low_completion,
       flag_capacity_drop,
       flag_capacity_spike,
       flag_low_utilization,
       flag_sampler_offline
FROM queue_forecast_daily_health
"""


DATASETS = {
    "runs": Dataset(
        file="runs.parquet",
        sql=_RUNS_SQL,
        params=("train_start", "as_of_date"),
        columns=(
            "task_id", "run_id", "pending_at", "started_at", "resolved_at",
            "reason_resolved", "wait_duration_s", "run_duration_s",
            "priority_at_pending", "queue_pending", "task_queue_id",
            "scheduler_id", "metadata_name", "normalized_name",
            "max_run_time_s", "repo_family", "tags",
        ),
        watermark_columns=("pending_at", "resolved_at"),
    ),
    "worker_counts": Dataset(
        file="worker_counts.parquet",
        sql=_WORKER_COUNTS_SQL,
        params=("window_lower", "as_of_date"),
        columns=("task_queue_id", "sampled_at", "running_workers",
                 "claimed_tasks", "existing_capacity"),
        watermark_columns=("sampled_at",),
    ),
    "worker_pools": Dataset(
        file="worker_pools.parquet",
        sql=_WORKER_POOLS_SQL,
        params=(),
        columns=("task_queue_id", "pool_kind", "provider_type"),
        watermark_columns=(),
    ),
    "throughput_runs": Dataset(
        file="throughput_runs.parquet",
        sql=_THROUGHPUT_SQL,
        params=("window_lower", "as_of_date"),
        columns=("task_queue_id", "started_at", "resolved_at",
                 "wait_duration_s", "run_duration_s"),
        watermark_columns=("resolved_at",),
    ),
    "qctx_runs": Dataset(
        file="qctx_runs.parquet",
        sql=_QCTX_SQL,
        params=("as_of_date", "ref_lower", "window_lower"),
        columns=("task_id", "run_id", "pending_at", "started_at",
                 "resolved_at", "priority_at_pending", "task_queue_id",
                 "repo_family"),
        watermark_columns=("pending_at", "resolved_at"),
    ),
    "daily_health": Dataset(
        file="daily_health.parquet",
        sql=_DAILY_HEALTH_SQL,
        params=(),
        columns=(
            "sample_date", "is_anomalous",
            "flag_exception_spike", "flag_stuck_pending_spike",
            "flag_wait_p99_spike", "flag_volume_anomaly",
            "flag_low_completion", "flag_capacity_drop", "flag_capacity_spike",
            "flag_low_utilization", "flag_sampler_offline",
        ),
        watermark_columns=("sample_date",),
    ),
}


def bindings(name, request):
    """The bound parameters for one dataset, taken ONLY from a validated request.

    `request[...]` rather than `request.get(...)`: a missing window field must
    raise here, not bind `None`. A `None` in a timestamp comparison makes every
    row fail the predicate, and the extract then looks like a correctly-executed
    query over an empty window -- an answer, not an error.

    Nothing outside the validated request can appear in the result: the names
    come from `DATASETS` and the values come from a mapping this function only
    ever indexes, never iterates.
    """
    return {param: request[param] for param in DATASETS[name].params}
