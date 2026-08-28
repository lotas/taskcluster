"""Closed-world validation and canonical hashing for extraction requests.

Phase 2b-1 Task 1. The sibling of `spec.py`, and the same two rules run through
it: unknown is refused rather than ignored, and no field ever becomes part of a
string that something else parses.

WHAT THIS MODULE IS DEFENDING (design D4, plan D17). The trainer's own loader
builds its query from a config that lives in `qf-research`:

    f"r.{c.target_column} AS y"                 # a column name from a config
    where = [..., *c.filters]                   # SQL fragments from a config

Trusted code that accepted either of those would defeat the claim that a new
table or column needs a human promotion, and would do it silently. So an
extraction request names a **target**, a **window**, a **lookback** and a
**generation** -- and the mapping from target to column lives here, in trusted
code, as a dict with two entries.

Pure, with one exception that is deliberate: `validate` takes `now` as an
argument rather than reading a clock, because the settlement-lag rule (D20) is a
comparison against the present and a module that reads its own clock cannot be
tested at a boundary.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import types

SCHEMA_VERSION = 1

# The target -> column mapping, and the entire closed world of targets.
# `configs/*.yaml` contains exactly these two `target_column` values, verified
# in the tree on 2026-08-27 (plan §4 fact 8). A third target is a human change
# here, which is the correct place for the friction.
TARGET_COLUMNS = {
    "wait_time": "wait_duration_s",
    "run_duration": "run_duration_s",
}

# `load_task_runs_for_queue_context` floors both sides of its join at
# `window_start - lookback_days`. Unbounded, that is a full-history scan of an
# ever-growing table -- the docstring records a confirmed multi-TB read profile.
LOOKBACK_MIN, LOOKBACK_MAX = 1, 120

# Bounded not because a large generation is harmful but because an unbounded
# integer in a closed-world validator is an inconsistency a later reader will
# take as permission.
GENERATION_MIN, GENERATION_MAX = 1, 1000

# HOW FAR BEFORE `train_start` ANY TRAILING OR REFERENCE WINDOW REACHES.
#
# One constant supersets all three prefixes the trainer uses, which are all
# different and none of which is 90 minutes when you follow the call:
#
#   qctx           `load_task_runs_for_queue_context(c, w.train_start - 90m, ..)`
#                  -- and `ref_lower` is derived from THAT, not from train_start
#   worker_counts  `load_worker_counts(c, w.train_start - 30m, ..)`, and the
#                  function subtracts a further 90m internally, so -120m
#   throughput     `train_start - (max(windows_minutes) + 30)m`, = -90m today
#
# A first version used `train_start` itself for the qctx bounds. That made the
# extract a SUBSET in two ways at once: the reference floor was 90 minutes late,
# and the pending-overlap predicate dropped any run that exited between
# `train_start - 90m` and `train_start` -- runs that affect queue-context
# features for the first rows of the window. A subset extract does not fail; it
# trains a slightly different model and reports a slightly different number.
#
# 24 hours because being generous here is nearly free: the window is months
# long, so a day of prefix is a fraction of a percent of rows, and
# `worker_counts` samples every five minutes, so it is 288 extra rows per queue.
WINDOW_LOOKBACK_MINUTES = 24 * 60

# The window itself is bounded, not just `lookback_days`. Bounding a part is not
# bounding the whole: `train_start` and `as_of_date` are supplied independently,
# so a caller could ask for 2010..2026 and get exactly the full-history scan the
# `lookback_days` ceiling exists to prevent.
#
# 60, LOWERED FROM 120 BY MEASUREMENT (2026-08-28). The first real extraction ran
# a 36-day window and the `runs` statement alone took 8 minutes against the role's
# 30-minute `statement_timeout`, which is enforced PER STATEMENT. Extrapolated:
#
#     36d ->  8.0 min (27% of the timeout, measured)
#     60d -> 13.3 min (44%)
#     90d -> 20.0 min (67%)
#    120d -> 26.7 min (89%)
#
# 120 was chosen for scan safety -- 3.3x the largest promoted config -- with no
# knowledge of runtime, and at 89% it would not reliably complete.
#
# 60 rather than 90, and the reason is growth: those figures are 36 days of
# TODAY'S volume, and the tables grow every day. A ceiling sitting at 67% of the
# timeout now becomes a ceiling over 100% of it later, silently, and the failure
# would look like a database problem rather than a bound nobody revisited.
#
# The ceiling's job is to bound an ACCIDENT, not to enable windows nobody needs:
# the largest promoted config spans 36 days (`run_duration.yaml`: lookback 30 +
# validation 1 + holdout 5), so 60 is still 1.7x the real requirement. Raising it
# requires a measurement at the new size, which is the correct friction.
MAX_WINDOW_DAYS = 60

# No forecasting data predates the project. The floor turns a typo into a
# refusal instead of a full-table scan, and it also keeps the derived bounds
# inside `datetime`'s representable range -- `datetime(1, 1, 1) - 30 days` raises
# OverflowError, which escaped as a crash rather than a refusal.
MIN_TRAIN_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

# D20: `as_of_date` must be a completed UTC day boundary AND far enough in the
# past that the collector has settled. There is no lateness SLA anywhere in the
# repository, so this number is an operational CHOICE, not a measurement, and
# saying so here is the honest version of shipping it. The first real
# extractions are what turn it into a measured value.
DEFAULT_SETTLEMENT_LAG_S = 48 * 3600

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})?\Z")

_REQUIRED = ("schema", "target", "train_start", "as_of_date", "lookback_days")
_OPTIONAL = ("generation",)

# Fields the request must NOT carry, each refused by name. They are listed
# rather than left to the unknown-key check so the message can say why: these
# are the ones somebody will reach for, and "unknown key" would read as a
# spelling mistake rather than as a rule.
_FORBIDDEN = {
    "target_column": "the column is derived from `target` in trusted code (D4)",
    "filters": "an extraction request carries no SQL (D4)",
    "ref_lower": "derived from window_lower and lookback_days",
    "window_lower": "derived from train_start in trusted code",
    "settlement_lag_s": "trusted configuration, not a request field (D17)",
    "flag_subset": "the whole daily_health row set is emitted; subset it in the"
                   " candidate (D17)",
    "columns": "the column inventory is fixed in trusted code (D18)",
    "config": "a trusted query never reads a research config (D4)",
}

UTC = datetime.timezone.utc


class OMIT:
    """Sentinel for tests that build a request with a field left out."""


class ExtractSpecError(ValueError):
    """An extraction request that must not be accepted.

    DELIBERATELY NOT a subclass of `spec.SpecError`, though one error family
    would be tidier. `spec.py` lives in `dispatcher/`, and this module is
    imported by BOTH privilege domains -- so subclassing it would make `shared`
    depend on `dispatcher`, which is the dependency direction this directory
    exists to forbid. A tidy hierarchy is worth less than a one-way dependency.

    The cost is one line in each consumer: `qfd` catches both this and
    `SpecError` on its refusal path (Task 5), and the extractor's service lists
    both in `SAFE_ERRORS`.
    """


def _err(msg):
    raise ExtractSpecError(msg)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_boundary(field, value):
    """An ISO-8601 UTC day boundary, or a refusal that says which rule broke.

    One regex could enforce all three rules at once (`\\dT00:00:00Z`), and it
    would tell the caller nothing about which of the three they broke.
    """
    if not isinstance(value, str):
        _err(f"{field} must be an ISO-8601 UTC timestamp string,"
             f" got {type(value).__name__}")
    m = _TS_RE.match(value)
    if not m:
        _err(f"{field} must look like 2026-08-01T00:00:00Z, got {value!r}")
    y, mo, d, hh, mm, ss, tz = m.groups()
    if tz is None:
        _err(f"{field} must be explicit UTC ending in Z, got a naive"
             f" timestamp {value!r}")
    if tz != "Z":
        # Not converted. A window expressed in a local zone has day boundaries
        # that depend on where the caller was sitting.
        _err(f"{field} must be UTC ending in Z, not an offset: {value!r}")
    if (hh, mm, ss) != ("00", "00", "00"):
        _err(f"{field} must be a completed UTC day boundary (T00:00:00Z),"
             f" got {value!r}")
    try:
        return datetime.datetime(int(y), int(mo), int(d), tzinfo=UTC)
    except ValueError as e:
        _err(f"{field} is not a real date: {value!r} ({e})")


def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(raw, *, now, settlement_lag_s=DEFAULT_SETTLEMENT_LAG_S):
    """Validate an extraction request and return the effective request.

    `now` is injected (an aware UTC datetime); `settlement_lag_s` is trusted
    configuration supplied by the caller's own config, never by the request.

    The result is a read-only mapping carrying every field, defaults included,
    because that is what runs and therefore what `request_hash` must cover.
    """
    if not isinstance(raw, dict):
        _err("an extraction request must be a JSON object")

    for name, why in _FORBIDDEN.items():
        if name in raw:
            _err(f"{name} is not a request field: {why}")

    unknown = set(raw) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        _err(f"unknown key(s): {sorted(unknown)}")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        _err(f"missing key(s): {missing}")

    if raw["schema"] != SCHEMA_VERSION or not _is_int(raw["schema"]):
        _err(f"schema must be {SCHEMA_VERSION}, got {raw['schema']!r}")

    # isinstance BEFORE membership: `x not in {}` on a list or a dict raises
    # TypeError("unhashable type"), and a validator that raises TypeError has
    # not refused the request, it has crashed on it -- the caller gets a
    # traceback instead of a reason and the refusal path never runs.
    target = raw["target"]
    if not isinstance(target, str):
        _err(f"target must be a string, got {type(target).__name__}")
    if target not in TARGET_COLUMNS:
        _err(f"unknown target {target!r}; known: {sorted(TARGET_COLUMNS)}")

    train_start = _parse_boundary("train_start", raw["train_start"])
    as_of_date = _parse_boundary("as_of_date", raw["as_of_date"])
    if train_start >= as_of_date:
        _err(f"train_start must be before as_of_date, got"
             f" {_fmt(train_start)} >= {_fmt(as_of_date)}")
    if train_start < MIN_TRAIN_START:
        _err(f"train_start {_fmt(train_start)} predates the data: nothing was"
             f" collected before {_fmt(MIN_TRAIN_START)}")
    span_days = (as_of_date - train_start).days
    if span_days > MAX_WINDOW_DAYS:
        _err(f"the window spans {span_days} days; the ceiling is"
             f" {MAX_WINDOW_DAYS}. The largest promoted config needs 36."
             f" A wider window is a full-history scan, which is what the"
             f" lookback bound exists to prevent.")

    # THE SETTLEMENT RULE (D20). The source tables change continuously -- the
    # collector runs a one-minute enrichment backfill -- so a window ending
    # inside the live tail cannot be made reproducible after the fact. It is
    # refused here rather than extracted and caveated.
    if not _is_int(settlement_lag_s) or settlement_lag_s < 0:
        _err("settlement_lag_s configuration must be a non-negative int")
    if now.tzinfo is None:
        _err("now must be an aware UTC datetime")
    latest = now.astimezone(UTC) - datetime.timedelta(seconds=settlement_lag_s)
    latest_boundary = datetime.datetime(latest.year, latest.month, latest.day,
                                        tzinfo=UTC)
    if as_of_date > latest_boundary:
        _err(f"as_of_date {_fmt(as_of_date)} is not settled: with a settlement"
             f" lag of {settlement_lag_s}s ({settlement_lag_s // 3600}h) the"
             f" latest extractable boundary is {_fmt(latest_boundary)}."
             f" The collector is still writing inside that window.")

    lookback_days = raw["lookback_days"]
    if not _is_int(lookback_days) \
            or not LOOKBACK_MIN <= lookback_days <= LOOKBACK_MAX:
        _err(f"lookback_days must be an int in"
             f" [{LOOKBACK_MIN},{LOOKBACK_MAX}], got {lookback_days!r}")

    generation = raw.get("generation", GENERATION_MIN)
    if not _is_int(generation) \
            or not GENERATION_MIN <= generation <= GENERATION_MAX:
        _err(f"generation must be an int in"
             f" [{GENERATION_MIN},{GENERATION_MAX}], got {generation!r}")

    # Both derived bounds hang off `window_lower`, matching the trainer, which
    # derives its reference floor from the shifted window start and not from
    # `train_start`. Wrapped because the arithmetic can leave `datetime`'s range
    # for an extreme input -- MIN_TRAIN_START makes that unreachable today, and
    # a backstop that costs three lines outlives the assumption that it is.
    try:
        window_lower = train_start - datetime.timedelta(
            minutes=WINDOW_LOOKBACK_MINUTES)
        ref_lower = window_lower - datetime.timedelta(days=lookback_days)
    except (OverflowError, OSError, ValueError) as e:
        _err(f"train_start {_fmt(train_start)} with lookback_days"
             f" {lookback_days} produces an unrepresentable bound ({e})")

    return _freeze({
        "schema": SCHEMA_VERSION,
        "target": target,
        "target_column": TARGET_COLUMNS[target],
        "train_start": _fmt(train_start),
        "as_of_date": _fmt(as_of_date),
        "lookback_days": lookback_days,
        # The earliest instant any trailing or reference window reaches.
        "window_lower": _fmt(window_lower),
        # Recorded, not left to be recomputed: this is the value that reaches
        # SQL, so the audit record should show what was queried rather than
        # what could be derived from what was asked. A change in the derivation
        # then shows up as a request-hash change instead of silently.
        "ref_lower": _fmt(ref_lower),
        "generation": generation,
    })


def _freeze(d):
    """A read-only view, so a later stage cannot widen a validated request.

    Every value here is a scalar, so a shallow proxy is genuinely immutable --
    there is no nested container to reach through. If a field ever becomes a
    list or a dict, this stops being sufficient and the test that pins the
    field set will be the thing that says so.
    """
    return types.MappingProxyType(d)


def canonical(effective):
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8.

    `dict()` because `json` serialises dicts, not arbitrary mappings, and the
    validated request is a proxy.
    """
    return json.dumps(dict(effective), sort_keys=True,
                      separators=(",", ":")).encode()


def request_hash(effective):
    """Names the request, and therefore the extract (D20).

    Deliberately NOT covering the settlement lag: the lag gates whether a
    window may be extracted, not what the extract contains. Hashing it would
    mean that adjusting an operational knob orphaned every published extract
    and re-extracted the whole history. The lag is recorded in the manifest as
    provenance instead.
    """
    return hashlib.sha256(canonical(effective)).hexdigest()
