#!/usr/bin/env python3
"""Counterfactual Taskcluster scheduling-policy simulator.

This is deliberately a single, standard-library-only file.  It consumes CSV
exports of queue_forecast_task_runs joined to queue_forecast_tasks and, for the
capacity mode, queue_forecast_worker_counts.

Examples:

  python3 scheduling-policy-simulator.py replay --runs runs.csv
  python3 scheduling-policy-simulator.py capacity --runs runs.csv \
      --capacity worker_counts.csv --policy strict,florian,oldest-every=5
  python3 scheduling-policy-simulator.py --print-sql

The replay mode treats every historical started_at as a claim opportunity.  The
capacity mode treats existing_capacity (or another selected column) as a
piecewise-constant slot limit and run_duration_s as service time.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


PRIORITIES = (
    "highest",
    "very-high",
    "high",
    "medium",
    "low",
    "very-low",
    "lowest",
)
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}
WAIT_THRESHOLDS = (("15m", 15 * 60), ("1h", 60 * 60), ("6h", 6 * 60 * 60))

RUNS_REQUIRED = {"task_id", "run_id", "task_queue_id", "priority_at_pending", "pending_at"}
CAPACITY_REQUIRED = {"task_queue_id", "sampled_at"}

SQL = r"""-- runs.csv (psql: \copy (...) TO 'runs.csv' CSV HEADER)
SELECT r.task_id, r.run_id, t.task_queue_id,
       COALESCE(r.priority_at_pending, t.original_priority) AS priority_at_pending,
       r.pending_at, r.started_at, r.resolved_at, r.reason_resolved,
       r.run_duration_s, t.normalized_name, t.max_run_time_s
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t USING (task_id)
WHERE r.pending_at >= :'from'::timestamptz
  AND r.pending_at <  :'until'::timestamptz
ORDER BY t.task_queue_id, r.pending_at, r.task_id, r.run_id;

-- worker_counts.csv (only needed by capacity mode)
SELECT task_queue_id, sampled_at, running_workers, claimed_tasks,
       existing_capacity
FROM queue_forecast_worker_counts
WHERE sampled_at >= :'from'::timestamptz
  AND sampled_at <= :'until'::timestamptz
ORDER BY task_queue_id, sampled_at;
"""


def parse_time(value: str | None, *, field_name: str) -> float | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field_name} timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone: {value!r}")
    return parsed.timestamp()


def format_time(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def finite_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    result = float(value)
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class Task:
    index: int
    task_id: str
    run_id: int
    queue: str
    priority: str
    pending: float
    historical_start: float | None
    resolved: float | None
    reason_resolved: str | None
    duration: float | None
    normalized_name: str | None
    max_run_time: float | None
    state: str = "future"
    simulated_start: float | None = None
    duration_imputed: bool = False

    @property
    def key(self) -> str:
        return f"{self.task_id}/{self.run_id}"


@dataclass(slots=True)
class CapacitySample:
    queue: str
    sampled: float
    capacity: int


@dataclass
class QueueState:
    by_priority: list[list[tuple[float, int, Task]]] = field(
        default_factory=lambda: [[] for _ in PRIORITIES]
    )
    oldest: list[tuple[float, int, Task]] = field(default_factory=list)
    policy_position: int = 0

    def add(self, task: Task) -> None:
        task.state = "pending"
        item = (task.pending, task.index, task)
        heapq.heappush(self.by_priority[PRIORITY_RANK[task.priority]], item)
        heapq.heappush(self.oldest, item)

    @staticmethod
    def _peek(heap: list[tuple[float, int, Task]]) -> Task | None:
        while heap and heap[0][2].state != "pending":
            heapq.heappop(heap)
        return heap[0][2] if heap else None

    def pop_priority(self, rank: int) -> Task | None:
        task = self._peek(self.by_priority[rank])
        if task is not None:
            heapq.heappop(self.by_priority[rank])
        return task

    def pop_strict(self, ranks: Iterable[int] = range(len(PRIORITIES))) -> Task | None:
        for rank in ranks:
            task = self.pop_priority(rank)
            if task is not None:
                return task
        return None

    def pop_oldest(self) -> Task | None:
        task = self._peek(self.oldest)
        if task is not None:
            heapq.heappop(self.oldest)
        return task

    def has_pending(self) -> bool:
        return self._peek(self.oldest) is not None


@dataclass(frozen=True, slots=True)
class Policy:
    label: str
    kind: str
    a: int = 0
    b: int = 0

    def select(self, state: QueueState) -> Task | None:
        if self.kind == "strict":
            return state.pop_strict()

        if self.kind == "oldest-every":
            # N=5 means four strict decisions followed by one global-oldest.
            use_oldest = state.policy_position == self.a - 1
            state.policy_position = (state.policy_position + 1) % self.a
            return state.pop_oldest() if use_oldest else state.pop_strict()

        if self.kind == "low-vlow":
            # highest..medium remain strict and do not consume a cycle position.
            task = state.pop_strict(range(PRIORITY_RANK["medium"] + 1))
            if task is not None:
                return task

            low_rank = PRIORITY_RANK["low"]
            vlow_rank = PRIORITY_RANK["very-low"]
            low = state._peek(state.by_priority[low_rank])
            vlow = state._peek(state.by_priority[vlow_rank])
            if low is not None or vlow is not None:
                cycle_length = self.a + self.b
                selected_rank = low_rank if state.policy_position < self.a else vlow_rank
                state.policy_position = (state.policy_position + 1) % cycle_length
                # Advancing even on fallback prevents unused credits accumulating.
                task = state.pop_priority(selected_rank)
                if task is None:
                    task = state.pop_priority(vlow_rank if selected_rank == low_rank else low_rank)
                return task

            # lowest is idle-only relative to every other class.
            return state.pop_priority(PRIORITY_RANK["lowest"])

        raise AssertionError(f"unknown policy kind {self.kind}")


def parse_policy(value: str) -> Policy:
    value = value.strip().lower()
    if value == "strict":
        return Policy("strict", "strict")
    if value == "florian":
        return Policy("florian-2:1", "low-vlow", 2, 1)
    if value.startswith("oldest-every="):
        try:
            every = int(value.split("=", 1)[1])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid policy {value!r}") from exc
        if every < 2:
            raise argparse.ArgumentTypeError("oldest-every must be at least 2")
        return Policy(f"oldest-every={every}", "oldest-every", every)
    if value.startswith("low-vlow="):
        try:
            low, vlow = (int(part) for part in value.split("=", 1)[1].split(":"))
        except (ValueError, TypeError) as exc:
            raise argparse.ArgumentTypeError("low-vlow policy must look like low-vlow=2:1") from exc
        if low < 1 or vlow < 1:
            raise argparse.ArgumentTypeError("low-vlow ratio parts must be positive")
        return Policy(f"low-vlow={low}:{vlow}", "low-vlow", low, vlow)
    raise argparse.ArgumentTypeError(
        f"unknown policy {value!r}; use strict, florian, oldest-every=N, or low-vlow=L:V"
    )


def policies_arg(value: str) -> list[Policy]:
    policies = [parse_policy(part) for part in value.split(",") if part.strip()]
    if not policies:
        raise argparse.ArgumentTypeError("at least one policy is required")
    labels = [policy.label for policy in policies]
    if len(labels) != len(set(labels)):
        raise argparse.ArgumentTypeError("policies must not be repeated")
    return policies


def load_tasks(path: Path, queue_filter: set[str] | None) -> list[Task]:
    tasks: list[Task] = []
    seen: set[tuple[str, int]] = set()
    ignored_terminal_without_priority = 0
    try:
        handle = path.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        missing = RUNS_REQUIRED - columns
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, 2):
            queue = row["task_queue_id"].strip()
            if not queue or (queue_filter is not None and queue not in queue_filter):
                continue
            try:
                run_id = int(row["run_id"])
                pending = parse_time(row["pending_at"], field_name="pending_at")
                started = parse_time(row.get("started_at"), field_name="started_at")
                resolved = parse_time(row.get("resolved_at"), field_name="resolved_at")
                duration = finite_float(row.get("run_duration_s"))
                max_run_time = finite_float(row.get("max_run_time_s"))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{row_number}: {exc}") from exc
            assert pending is not None
            priority = row["priority_at_pending"].strip()
            # The collector can create a zero-lifetime pending record from a
            # deadline-exceeded event without ever observing its priority.  It
            # could not have competed at a claim opportunity, so omitting it is
            # exact rather than an imputation.
            if (
                not priority and started is None and resolved is not None
                and resolved <= pending
            ):
                ignored_terminal_without_priority += 1
                continue
            # Taskcluster accepts the legacy API spelling but queues it at the
            # modern default priority.
            if priority == "normal":
                priority = "lowest"
            if priority not in PRIORITY_RANK:
                raise ValueError(
                    f"{path}:{row_number}: unknown priority {priority!r}; "
                    "use COALESCE(priority_at_pending, original_priority) in the export"
                )
            key = (row["task_id"], run_id)
            if key in seen:
                raise ValueError(f"{path}:{row_number}: duplicate run {key[0]}/{key[1]}")
            seen.add(key)
            if started is not None and started < pending:
                raise ValueError(f"{path}:{row_number}: started_at precedes pending_at")
            if resolved is not None and resolved < pending:
                raise ValueError(f"{path}:{row_number}: resolved_at precedes pending_at")
            tasks.append(Task(
                index=len(tasks), task_id=row["task_id"], run_id=run_id, queue=queue,
                priority=priority, pending=pending, historical_start=started,
                resolved=resolved, reason_resolved=row.get("reason_resolved") or None,
                duration=duration, normalized_name=row.get("normalized_name") or None,
                max_run_time=max_run_time,
            ))
    if not tasks:
        raise ValueError("no tasks remain after reading input and applying queue filters")
    if ignored_terminal_without_priority:
        print(
            f"warning: ignored {ignored_terminal_without_priority} zero-lifetime terminal "
            "runs with no recorded priority",
            file=sys.stderr,
        )
    return tasks


def load_capacity(path: Path, column: str, queue_filter: set[str] | None) -> list[CapacitySample]:
    samples: list[CapacitySample] = []
    try:
        handle = path.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        missing = CAPACITY_REQUIRED - columns
        if missing or column not in columns:
            missing_with_column = missing | ({column} if column not in columns else set())
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing_with_column))}")
        for row_number, row in enumerate(reader, 2):
            queue = row["task_queue_id"].strip()
            if not queue or (queue_filter is not None and queue not in queue_filter):
                continue
            value = finite_float(row.get(column))
            if value is None:
                continue
            if value < 0 or not value.is_integer():
                raise ValueError(f"{path}:{row_number}: {column} must be a nonnegative integer")
            sampled = parse_time(row["sampled_at"], field_name="sampled_at")
            assert sampled is not None
            samples.append(CapacitySample(queue, sampled, int(value)))
    if not samples:
        raise ValueError(f"no usable {column} samples remain after filtering")
    samples.sort(key=lambda sample: (sample.queue, sample.sampled))
    return samples


def fresh_tasks(tasks: Sequence[Task]) -> list[Task]:
    return [Task(
        index=task.index, task_id=task.task_id, run_id=task.run_id, queue=task.queue,
        priority=task.priority, pending=task.pending,
        historical_start=task.historical_start, resolved=task.resolved,
        reason_resolved=task.reason_resolved, duration=task.duration,
        normalized_name=task.normalized_name, max_run_time=task.max_run_time,
    ) for task in tasks]


def pending_withdrawal(task: Task) -> float | None:
    # A terminal event for a run that historically never started is useful and
    # causally plausible as a pending cancellation/deadline.  A resolved_at for
    # a historically-started task depends on that historical start and must not
    # remove it from a counterfactual pending queue.
    return task.resolved if task.historical_start is None else None


def activate_until(
    tasks: Sequence[Task], cursor: int, now: float, state: QueueState,
) -> int:
    while cursor < len(tasks) and tasks[cursor].pending <= now:
        state.add(tasks[cursor])
        cursor += 1
    return cursor


def withdraw_until(withdrawals: list[tuple[float, int, Task]], now: float) -> int:
    count = 0
    while withdrawals and withdrawals[0][0] <= now:
        _, _, task = heapq.heappop(withdrawals)
        if task.state == "pending":
            task.state = "withdrawn"
            count += 1
    return count


def start_task(task: Task, now: float) -> None:
    task.state = "started"
    task.simulated_start = now


@dataclass(slots=True)
class SimulationResult:
    mode: str
    policy: str
    tasks: list[Task]
    horizon: float
    opportunities: int = 0
    idle_opportunities: int = 0
    validation_matches: int = 0
    validation_comparable: int = 0
    completions: int = 0
    imputed_starts: int = 0


def simulate_replay(source_tasks: Sequence[Task], policy: Policy, end: float | None) -> SimulationResult:
    tasks = fresh_tasks(source_tasks)
    opportunities = sorted(
        (task.historical_start, task.queue, task.key)
        for task in tasks if task.historical_start is not None
    )
    if not opportunities:
        raise ValueError("replay mode needs at least one nonempty started_at")
    horizon = end if end is not None else opportunities[-1][0]
    assert horizon is not None
    if horizon < opportunities[0][0]:
        raise ValueError("--end precedes the first start opportunity")

    by_queue: dict[str, list[Task]] = defaultdict(list)
    opps_by_queue: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for task in tasks:
        by_queue[task.queue].append(task)
    for when, queue, historical_key in opportunities:
        if when <= horizon:
            opps_by_queue[queue].append((when, historical_key))

    result = SimulationResult("replay", policy.label, tasks, horizon)
    for queue, queue_tasks in by_queue.items():
        queue_tasks.sort(key=lambda task: (task.pending, task.index))
        state = QueueState()
        withdrawals = [
            (when, task.index, task) for task in queue_tasks
            if (when := pending_withdrawal(task)) is not None
        ]
        heapq.heapify(withdrawals)
        cursor = 0
        queue_opportunities = opps_by_queue.get(queue, [])
        opportunity_cursor = 0
        while opportunity_cursor < len(queue_opportunities):
            now = queue_opportunities[opportunity_cursor][0]
            historical_keys: set[str] = set()
            while (
                opportunity_cursor < len(queue_opportunities)
                and queue_opportunities[opportunity_cursor][0] == now
            ):
                historical_keys.add(queue_opportunities[opportunity_cursor][1])
                opportunity_cursor += 1
            cursor = activate_until(queue_tasks, cursor, now, state)
            withdraw_until(withdrawals, now)
            simulated_keys: set[str] = set()
            for _ in historical_keys:
                selected = policy.select(state)
                result.opportunities += 1
                if selected is None:
                    result.idle_opportunities += 1
                    continue
                start_task(selected, now)
                simulated_keys.add(selected.key)
            result.validation_comparable += len(simulated_keys)
            result.validation_matches += len(simulated_keys & historical_keys)
        activate_until(queue_tasks, cursor, horizon, state)
        withdraw_until(withdrawals, horizon)
    return result


def median(values: Iterable[float]) -> float | None:
    materialized = [value for value in values if value is not None and value >= 0]
    return statistics.median(materialized) if materialized else None


def impute_durations(tasks: Sequence[Task], method: str) -> int:
    missing = [task for task in tasks if task.duration is None or task.duration < 0]
    if not missing:
        return 0
    if method == "error":
        examples = ", ".join(task.key for task in missing[:3])
        raise ValueError(f"capacity mode has {len(missing)} missing durations (for example {examples})")
    if method == "max-run-time":
        unavailable = [task for task in missing if task.max_run_time is None or task.max_run_time < 0]
        if unavailable:
            raise ValueError(f"{len(unavailable)} missing-duration tasks also lack max_run_time_s")
        for task in missing:
            task.duration = task.max_run_time
            task.duration_imputed = True
        return len(missing)

    known = [task for task in tasks if task.duration is not None and task.duration >= 0]
    global_median = median(task.duration for task in known)
    if global_median is None:
        raise ValueError("cannot median-impute durations: input has no observed duration")
    by_name: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_priority: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_queue: dict[str, list[float]] = defaultdict(list)
    for task in known:
        assert task.duration is not None
        if task.normalized_name:
            by_name[(task.queue, task.normalized_name)].append(task.duration)
        by_priority[(task.queue, task.priority)].append(task.duration)
        by_queue[task.queue].append(task.duration)
    name_medians = {key: statistics.median(values) for key, values in by_name.items()}
    priority_medians = {key: statistics.median(values) for key, values in by_priority.items()}
    queue_medians = {key: statistics.median(values) for key, values in by_queue.items()}
    for task in missing:
        task.duration = (
            name_medians.get((task.queue, task.normalized_name)) if task.normalized_name else None
        )
        if task.duration is None:
            task.duration = priority_medians.get((task.queue, task.priority))
        if task.duration is None:
            task.duration = queue_medians.get(task.queue, global_median)
        task.duration_imputed = True
    return len(missing)


def simulate_capacity(
    source_tasks: Sequence[Task], samples: Sequence[CapacitySample], policy: Policy,
    end: float | None, missing_duration: str,
) -> SimulationResult:
    tasks = fresh_tasks(source_tasks)
    impute_durations(tasks, missing_duration)
    by_queue: dict[str, list[Task]] = defaultdict(list)
    samples_by_queue: dict[str, list[CapacitySample]] = defaultdict(list)
    for task in tasks:
        by_queue[task.queue].append(task)
    for sample in samples:
        samples_by_queue[sample.queue].append(sample)
    absent = sorted(set(by_queue) - set(samples_by_queue))
    if absent:
        preview = ", ".join(absent[:5])
        raise ValueError(f"no capacity samples for {len(absent)} task queues: {preview}")
    default_horizon = max(sample.sampled for sample in samples)
    horizon = end if end is not None else default_horizon
    if horizon < min(sample.sampled for sample in samples):
        raise ValueError("--end precedes every capacity sample")
    result = SimulationResult("capacity", policy.label, tasks, horizon)

    for queue, queue_tasks in by_queue.items():
        queue_tasks.sort(key=lambda task: (task.pending, task.index))
        queue_samples = samples_by_queue[queue]
        state = QueueState()
        withdrawals = [
            (when, task.index, task) for task in queue_tasks
            if (when := pending_withdrawal(task)) is not None
        ]
        heapq.heapify(withdrawals)
        completions: list[tuple[float, int]] = []
        cursor = sample_cursor = busy = capacity = 0
        infinity = float("inf")

        def fill(now: float) -> None:
            nonlocal busy
            while busy < capacity:
                selected = policy.select(state)
                if selected is None:
                    break
                start_task(selected, now)
                assert selected.duration is not None
                heapq.heappush(completions, (now + selected.duration, selected.index))
                busy += 1
                result.imputed_starts += int(selected.duration_imputed)

        while True:
            next_arrival = queue_tasks[cursor].pending if cursor < len(queue_tasks) else infinity
            next_sample = (
                queue_samples[sample_cursor].sampled
                if sample_cursor < len(queue_samples) else infinity
            )
            next_completion = completions[0][0] if completions else infinity
            next_withdrawal = withdrawals[0][0] if withdrawals else infinity
            now = min(next_arrival, next_sample, next_completion, next_withdrawal)
            if now == infinity or now > horizon:
                break
            while completions and completions[0][0] <= now:
                heapq.heappop(completions)
                busy -= 1
                result.completions += 1
            cursor = activate_until(queue_tasks, cursor, now, state)
            withdraw_until(withdrawals, now)
            while sample_cursor < len(queue_samples) and queue_samples[sample_cursor].sampled <= now:
                capacity = queue_samples[sample_cursor].capacity
                sample_cursor += 1
            fill(now)
    return result


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def metric_rows(result: SimulationResult) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Task]] = defaultdict(list)
    for task in result.tasks:
        if task.pending <= result.horizon:
            groups[(task.queue, task.priority)].append(task)
            groups[(task.queue, "ALL")].append(task)
    rows: list[dict[str, object]] = []
    for (queue, priority), tasks in sorted(groups.items()):
        started = [task for task in tasks if task.simulated_start is not None]
        pending = [task for task in tasks if task.state == "pending"]
        withdrawn = [task for task in tasks if task.state == "withdrawn"]
        waits = [task.simulated_start - task.pending for task in started]  # type: ignore[operator]
        observed_worker_time = sum(
            task.duration for task in started if task.duration is not None and task.duration >= 0
        )
        known_duration_starts = sum(task.duration is not None and task.duration >= 0 for task in started)
        row: dict[str, object] = {
            "mode": result.mode,
            "policy": result.policy,
            "task_queue_id": queue,
            "priority": priority,
            "arrived": len(tasks),
            "started": len(started),
            "still_pending": len(pending),
            "withdrawn_while_pending": len(withdrawn),
            "wait_p50_s": percentile(waits, 0.50),
            "wait_p90_s": percentile(waits, 0.90),
            "wait_p99_s": percentile(waits, 0.99),
            "wait_max_s": max(waits) if waits else None,
            "max_backlog_age_s": max((result.horizon - task.pending for task in pending), default=None),
            "observed_worker_time_s": observed_worker_time,
            "known_duration_starts": known_duration_starts,
        }
        denominator = len(started) + len(pending)
        for label, threshold in WAIT_THRESHOLDS:
            over = sum(wait > threshold for wait in waits)
            over += sum(result.horizon - task.pending > threshold for task in pending)
            row[f"pct_wait_gt_{label}"] = (100.0 * over / denominator) if denominator else None
        rows.append(row)

    # Worker-time share is meaningful within a queue, not across unrelated pools.
    totals = {
        row["task_queue_id"]: row["observed_worker_time_s"]
        for row in rows if row["priority"] == "ALL"
    }
    for row in rows:
        total = totals[row["task_queue_id"]]
        row["worker_time_share_pct"] = (
            100.0 * row["observed_worker_time_s"] / total if total else None
        )
    return rows


def summary(result: SimulationResult) -> dict[str, object]:
    data: dict[str, object] = {
        "mode": result.mode,
        "policy": result.policy,
        "horizon": format_time(result.horizon),
        "tasks": len(result.tasks),
        "started": sum(task.simulated_start is not None for task in result.tasks),
        "still_pending": sum(task.state == "pending" for task in result.tasks),
        "withdrawn_while_pending": sum(task.state == "withdrawn" for task in result.tasks),
    }
    if result.mode == "replay":
        data.update({
            "opportunities": result.opportunities,
            "idle_opportunities": result.idle_opportunities,
            "strict_replay_matches": result.validation_matches if result.policy == "strict" else None,
            "strict_replay_comparable": result.validation_comparable if result.policy == "strict" else None,
            "strict_replay_match_pct": (
                100.0 * result.validation_matches / result.validation_comparable
                if result.policy == "strict" and result.validation_comparable else None
            ),
        })
    else:
        data.update({
            "completed_within_horizon": result.completions,
            "durations_imputed": sum(task.duration_imputed for task in result.tasks),
            "starts_using_imputed_duration": result.imputed_starts,
        })
    return data


def add_strict_deltas(metrics: list[dict[str, object]]) -> None:
    baseline = {
        (row["mode"], row["task_queue_id"], row["priority"]): row
        for row in metrics if row["policy"] == "strict"
    }
    comparisons = (
        ("wait_p90_s", "vs_strict_wait_p90_delta_s"),
        ("still_pending", "vs_strict_still_pending_delta"),
        ("worker_time_share_pct", "vs_strict_worker_time_share_delta_pp"),
    )
    for row in metrics:
        strict = baseline.get((row["mode"], row["task_queue_id"], row["priority"]))
        for source, destination in comparisons:
            left = row[source]
            right = strict[source] if strict is not None else None
            row[destination] = (
                left - right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
            )


def output_comparison(metrics: Sequence[dict[str, object]]) -> None:
    rows = [
        row for row in metrics
        if row["policy"] != "strict" and row["priority"] != "ALL"
        and row.get("vs_strict_wait_p90_delta_s") is not None
    ]
    if not rows:
        return
    print("\nCounterfactual deltas vs strict (negative wait/pending is an improvement)")
    columns = (
        "policy", "task_queue_id", "priority", "vs_strict_wait_p90_delta_s",
        "vs_strict_still_pending_delta", "vs_strict_worker_time_share_delta_pp",
    )
    widths = {
        column: max(len(column), *(len(printable(row[column])) for row in rows))
        for column in columns
    }
    print("  ".join(column.ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(printable(row[column]).ljust(widths[column]) for column in columns))


def printable(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def output_text(result: SimulationResult, rows: Sequence[dict[str, object]]) -> None:
    info = summary(result)
    print(f"\n{result.mode} / {result.policy} / horizon {info['horizon']}")
    fields = [
        f"started={info['started']}", f"pending={info['still_pending']}",
        f"withdrawn={info['withdrawn_while_pending']}",
    ]
    if result.mode == "replay":
        fields.extend((
            f"opportunities={info['opportunities']}",
            f"idle={info['idle_opportunities']}",
        ))
        if result.policy == "strict":
            fields.append(f"strict_match={printable(info['strict_replay_match_pct'])}%")
    else:
        fields.extend((
            f"completed={info['completed_within_horizon']}",
            f"durations_imputed={info['durations_imputed']}",
            f"imputed_starts={info['starts_using_imputed_duration']}",
        ))
    print("  " + "  ".join(fields))
    columns = (
        "task_queue_id", "priority", "arrived", "started", "still_pending",
        "wait_p50_s", "wait_p90_s", "wait_p99_s", "wait_max_s",
        "pct_wait_gt_1h", "max_backlog_age_s", "worker_time_share_pct",
    )
    widths = {
        column: max(len(column), *(len(printable(row[column])) for row in rows))
        for column in columns
    }
    print("  ".join(column.ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(printable(row[column]).ljust(widths[column]) for column in columns))


def write_details(directory: Path, results: Sequence[SimulationResult]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for result in results:
        safe_policy = result.policy.replace(":", "-").replace("=", "-")
        path = directory / f"{result.mode}-{safe_policy}-starts.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            fields = (
                "task_id", "run_id", "task_queue_id", "priority", "pending_at",
                "historical_started_at", "simulated_started_at", "simulated_wait_s",
                "run_duration_s", "duration_imputed", "state",
            )
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for task in result.tasks:
                writer.writerow({
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "task_queue_id": task.queue,
                    "priority": task.priority,
                    "pending_at": format_time(task.pending),
                    "historical_started_at": (
                        format_time(task.historical_start) if task.historical_start is not None else ""
                    ),
                    "simulated_started_at": (
                        format_time(task.simulated_start) if task.simulated_start is not None else ""
                    ),
                    "simulated_wait_s": (
                        task.simulated_start - task.pending if task.simulated_start is not None else ""
                    ),
                    "run_duration_s": task.duration if task.duration is not None else "",
                    "duration_imputed": task.duration_imputed,
                    "state": task.state,
                })


def self_test() -> None:
    def task(index: int, priority: str, pending: float, started: float | None = None, duration: float = 10) -> Task:
        return Task(index, f"t{index}", 0, "q", priority, pending, started, None, None, duration, None, None)

    # Strict policy reproduces a simple historical ordering.
    replay_tasks = [task(0, "low", 0, 10), task(1, "very-low", 0, 30), task(2, "low", 5, 20)]
    strict = simulate_replay(replay_tasks, parse_policy("strict"), 30)
    assert [t.task_id for t in strict.tasks if t.simulated_start == 10] == ["t0"]
    assert strict.validation_matches == 3

    # Florian's cycle does not starve very-low and higher classes preempt it.
    mixed = [task(i, "low" if i < 6 else "very-low", 0, float(10 + i)) for i in range(9)]
    florian = simulate_replay(mixed, parse_policy("florian"), 18)
    starts = sorted((t.simulated_start, t.priority) for t in florian.tasks if t.simulated_start is not None)
    assert [priority for _, priority in starts[:6]] == ["low", "low", "very-low"] * 2

    with_high = [task(0, "low", 0, 10), task(1, "very-low", 0, 20), task(2, "medium", 0, 30)]
    result = simulate_replay(with_high, parse_policy("florian"), 30)
    assert next(t for t in result.tasks if t.simulated_start == 10).priority == "medium"

    # Capacity feedback: one slot starts a second task only after completion.
    cap_tasks = [task(0, "low", 0, duration=10), task(1, "very-low", 0, duration=10)]
    samples = [CapacitySample("q", 0, 1), CapacitySample("q", 30, 1)]
    capacity = simulate_capacity(cap_tasks, samples, parse_policy("strict"), 30, "error")
    assert sorted(t.simulated_start for t in capacity.tasks) == [0, 10]
    print("self-test: ok")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare Taskcluster queue selection policies using historical CSV exports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Policies:
  strict          priority DESC, pending_at ASC
  florian         strict highest..medium; low,low,very-low; lowest idle-only
  low-vlow=L:V   generalized low/very-low ratio, for example low-vlow=3:1
  oldest-every=N N-1 strict selections then one global-oldest (5 is 80/20,
                  10 is 90/10, and 4 is 3:1)

Pending runs that historically resolved without starting are withdrawn at their
resolved_at. resolved_at is deliberately ignored for historically started runs
because it is downstream of the historical scheduling decision.
""",
    )
    parser.add_argument("--print-sql", action="store_true", help="print PostgreSQL CSV export queries and exit")
    parser.add_argument("--self-test", action="store_true", help="run embedded deterministic checks and exit")
    subparsers = parser.add_subparsers(dest="mode")

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--runs", type=Path, required=True, help="run/task CSV export")
        subparser.add_argument(
            "--policy", type=policies_arg,
            default=policies_arg("strict,florian,oldest-every=5"),
            help="comma-separated policies (default: strict,florian,oldest-every=5)",
        )
        subparser.add_argument("--queue", action="append", help="task_queue_id to include; repeatable")
        subparser.add_argument("--end", help="simulation horizon as timezone-aware ISO timestamp")
        subparser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        subparser.add_argument("--details-dir", type=Path, help="write per-run counterfactual start CSVs")

    replay = subparsers.add_parser("replay", help="replay historical started_at claim opportunities")
    common(replay)
    capacity = subparsers.add_parser("capacity", help="discrete-event capacity and duration simulation")
    common(capacity)
    capacity.add_argument("--capacity", type=Path, required=True, help="worker-count CSV export")
    capacity.add_argument(
        "--capacity-column", default="existing_capacity",
        help="slot-limit column (default: existing_capacity)",
    )
    capacity.add_argument(
        "--missing-duration", choices=("median", "max-run-time", "error"), default="median",
        help="handling for unobserved run_duration_s (default: hierarchical median)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.print_sql:
        print(SQL)
        return 0
    if args.self_test:
        self_test()
        return 0
    if args.mode is None:
        parser.error("choose replay or capacity (or use --print-sql / --self-test)")
    try:
        queue_filter = set(args.queue) if args.queue else None
        tasks = load_tasks(args.runs, queue_filter)
        end = parse_time(args.end, field_name="--end") if args.end else None
        if args.mode == "replay":
            def simulator(policy: Policy) -> SimulationResult:
                return simulate_replay(tasks, policy, end)
        else:
            samples = load_capacity(args.capacity, args.capacity_column, queue_filter)
            if args.capacity_column == "existing_capacity":
                print(
                    "warning: existing_capacity is Worker Manager currentCapacity, "
                    "not a guaranteed executable-slot count",
                    file=sys.stderr,
                )
            def simulator(policy: Policy) -> SimulationResult:
                return simulate_capacity(tasks, samples, policy, end, args.missing_duration)
        json_summaries: list[dict[str, object]] = []
        json_metrics: list[dict[str, object]] = []
        for policy in args.policy:
            result = simulator(policy)
            result_metrics = metric_rows(result)
            json_summaries.append(summary(result))
            json_metrics.extend(result_metrics)
            if args.details_dir:
                write_details(args.details_dir, [result])
            if not args.json:
                output_text(result, result_metrics)
        add_strict_deltas(json_metrics)
        if args.json:
            print(json.dumps(
                {"summaries": json_summaries, "metrics": json_metrics},
                indent=2, sort_keys=True,
            ))
        else:
            output_comparison(json_metrics)
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
