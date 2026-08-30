"""Read the trainer's six datasets from a frozen extract instead of Postgres.

WHY THIS EXISTS. `data_loader.py` reaches for `os.environ["DATABASE_URL"]` in six
places. A probe runs with `--network none` and no credential, so under the
sandbox the trainer does not fail to find data -- it raises `KeyError` before it
starts. This module is the other source: the six Parquet files the trusted
extractor publishes (`host/extractor/inventory.py`), which were derived from
those same six queries and carry a **superset** of what each one selects.

THE SUPERSET IS THE WHOLE DESIGN, and it is what this module has to close. Each
extract file is the widest set of columns and the widest window any config could
ask for, unfiltered. So for every loader, this module has to do in pandas exactly
what that loader's SQL did in Postgres:

  * narrow the window to what the config's `compute_windows` asked for,
  * apply the config's `filters` (raw SQL predicates -- see `_parse`),
  * project the columns that loader's SELECT listed, no more,
  * rename the target column to `y`, as `_build_query` does.

A step skipped here does not fail. It trains a slightly different model and
reports a slightly different number, which is the failure mode this whole path
exists to avoid -- so each of the four is asserted in `tests/test_extract_source.py`
against the SQL it mirrors.

NO CACHE, DELIBERATELY. Every DB-backed loader writes its result to
`data/cache/*.parquet` and reads it back on the next run. This path writes
nothing and reads nothing from there, for a reason that is not laziness: a cache
file's name encodes the config and the window but not the *source*, so an
extract-derived frame and a database-derived frame land on the same path and are
indistinguishable afterwards. The extract already IS an immutable, content-hashed
cache. Re-filtering it costs seconds against a training run's minutes.

WHAT IS VERIFIED AND WHAT IS NOT. Every file this module reads is checked against
the `sha256` the manifest recorded for it, once, before the read (disable with
`QF_EXTRACT_VERIFY=0` only if you can afford to be wrong about which bytes you
trained on). The manifest's own `extract_hash` is NOT recomputed: the canonical
hashing lives in the trusted `host/shared/extract_manifest.py`, which is not
present in the research repo, and a second implementation of a hash is a second
answer. The per-file digests are the check that matters here and they are
self-contained.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from pathlib import Path

import pandas as pd

ENV_DIR = "QF_EXTRACT_DIR"
ENV_VERIFY = "QF_EXTRACT_VERIFY"
MANIFEST_NAME = "MANIFEST.json"


class ExtractError(RuntimeError):
    """The extract cannot answer what was asked of it.

    Always raised, never worked around: every caller of this module has a
    Postgres path it would otherwise take, and silently falling back to a
    database from inside a run that asked for a frozen extract would produce a
    result whose provenance is a guess.
    """


# --- selection ---------------------------------------------------------------
_configured: Path | None = None
_sources: dict[Path, "ExtractSource"] = {}


def configure(path: str | Path | None) -> None:
    """Point the trainer at an extract directory, overriding `$QF_EXTRACT_DIR`.

    `None` clears an override and returns to the environment.
    """
    global _configured
    _configured = Path(path).resolve() if path is not None else None


def active() -> "ExtractSource | None":
    """The extract source in force, or `None` when the DB path should be taken.

    Memoised per directory so the manifest is parsed and each file's digest is
    verified once per process rather than once per loader.
    """
    root = _configured
    if root is None:
        env = os.environ.get(ENV_DIR)
        if not env:
            return None
        root = Path(env).resolve()
    if root not in _sources:
        _sources[root] = ExtractSource(root)
    return _sources[root]


def _reset_for_tests() -> None:
    global _configured
    _configured = None
    _sources.clear()


# --- SQL predicate translation ----------------------------------------------
#
# `c.filters` are raw SQL fragments, spliced into the loader's WHERE clause. On
# this path there is no SQL to splice them into, so they are parsed into a tiny
# AST and evaluated against the frame.
#
# THE GRAMMAR IS CLOSED AND FAIL-CLOSED. It covers exactly the shapes the
# promoted configs use, and anything else raises rather than being approximated.
# A predicate this module quietly ignored would widen the training population
# without changing a single number that anybody reads.
_IDENT = r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)"
_NULL_RE = re.compile(rf"^{_IDENT}\s+IS\s+(NOT\s+)?NULL$", re.I)
_CMP_RE = re.compile(rf"^{_IDENT}\s*(=|!=|<>|<=|>=|<|>)\s*(.+)$", re.I)
_IN_RE = re.compile(rf"^{_IDENT}\s+(NOT\s+)?IN\s*\((.+)\)$", re.I)
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_STR_RE = re.compile(r"^'((?:[^']|'')*)'$")


def _split_top_level(text: str) -> tuple[str, list[str]] | None:
    """Split on top-level `AND`/`OR`, respecting parens and quoted literals.

    Returns `(connective, parts)`, or `None` when there is no top-level
    connective. Mixing `AND` and `OR` at one level raises: SQL's precedence
    would resolve it and a reader's eye would not, and this module is not the
    place to be subtle about which rows are in the training set.
    """
    depth = 0
    in_str = False
    parts: list[str] = []
    found: set[str] = set()
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            in_str = not (ch == "'")
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ExtractError(f"unbalanced ')' in filter: {text!r}")
        elif depth == 0:
            m = re.match(r"\s+(AND|OR)\s+", text[i:], re.I)
            if m:
                parts.append(text[start:i])
                found.add(m.group(1).upper())
                i += m.end()
                start = i
                continue
        i += 1
    if in_str or depth != 0:
        raise ExtractError(f"unterminated string or paren in filter: {text!r}")
    if not parts:
        return None
    if len(found) > 1:
        raise ExtractError(
            f"filter mixes AND and OR at the same level: {text!r}."
            f" Parenthesise the intended grouping -- this module refuses to"
            f" pick a precedence on your behalf."
        )
    parts.append(text[start:])
    return found.pop(), [p.strip() for p in parts]


def _literal(text: str):
    text = text.strip()
    if _NUM_RE.match(text):
        return float(text) if any(c in text for c in ".eE") else int(text)
    m = _STR_RE.match(text)
    if m:
        return m.group(1).replace("''", "'")
    if text.upper() in ("TRUE", "FALSE"):
        return text.upper() == "TRUE"
    raise ExtractError(
        f"unsupported literal in filter: {text!r}. Supported: numbers,"
        f" single-quoted strings, TRUE/FALSE."
    )


def _parse(pred: str):
    """One SQL predicate -> AST node. See the grammar note above."""
    text = " ".join(pred.split()).strip()
    if not text:
        raise ExtractError("empty filter predicate")

    split = _split_top_level(text)
    if split is not None:
        connective, parts = split
        return (connective.lower(), [_parse(p) for p in parts])

    # A fully-parenthesised predicate with no top-level connective: unwrap it.
    if text.startswith("(") and text.endswith(")"):
        return _parse(text[1:-1])

    m = _NULL_RE.match(text)
    if m:
        return ("null", m.group(1), m.group(2) is not None)  # (col, negated)
    m = _IN_RE.match(text)
    if m:
        items = _split_list(m.group(3))
        return ("in", m.group(1), [_literal(v) for v in items],
                m.group(2) is not None)
    m = _CMP_RE.match(text)
    if m:
        return ("cmp", m.group(1), m.group(2), _literal(m.group(3)))
    raise ExtractError(
        f"unsupported filter predicate: {pred!r}. This path understands"
        f" `col IS [NOT] NULL`, `col <op> literal`, `col [NOT] IN (..)` and"
        f" AND/OR groups of those. Anything else is refused rather than"
        f" approximated -- a predicate applied loosely changes the training"
        f" population without changing any reported number."
    )


def _split_list(text: str) -> list[str]:
    """Comma-split an IN list, respecting quoted literals."""
    items: list[str] = []
    in_str = False
    start = 0
    for i, ch in enumerate(text):
        if in_str:
            in_str = not (ch == "'")
        elif ch == "'":
            in_str = True
        elif ch == ",":
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    if in_str:
        raise ExtractError(f"unterminated string in IN list: {text!r}")
    return [i for i in (s.strip() for s in items) if i]


def filter_columns(filters) -> set[str]:
    """Every column the filters reference, so the reader can project them."""
    cols: set[str] = set()

    def walk(node):
        if node[0] in ("and", "or"):
            for child in node[1]:
                walk(child)
        else:
            cols.add(node[1])

    for pred in filters:
        walk(_parse(pred))
    return cols


def _evaluate(node, df: pd.DataFrame) -> pd.Series:
    kind = node[0]
    if kind in ("and", "or"):
        mask = _evaluate(node[1][0], df)
        for child in node[1][1:]:
            mask = (mask & _evaluate(child, df)) if kind == "and" \
                else (mask | _evaluate(child, df))
        return mask

    col = node[1]
    if col not in df.columns:
        raise ExtractError(
            f"filter references column {col!r}, which the extract does not"
            f" carry. The extract's columns are fixed by trusted code"
            f" (host/extractor/inventory.py); a new one needs a human there."
        )
    series = df[col]

    if kind == "null":
        negated = node[2]
        return series.notna() if negated else series.isna()

    if kind == "in":
        values, negated = node[2], node[3]
        hit = series.isin(values)
        # SQL: `NULL NOT IN (..)` is UNKNOWN, so a NULL row is excluded either
        # way. pandas' `~isin` would keep it.
        return (~hit & series.notna()) if negated else hit

    op, value = node[2], node[3]
    # SQL comparisons against NULL are UNKNOWN -> row excluded. pandas
    # comparisons against NaN are False -> row excluded. Same outcome, which is
    # why no explicit NULL guard is needed here.
    ops = {
        "=":  lambda s: s == value,
        "!=": lambda s: (s != value) & s.notna(),
        "<>": lambda s: (s != value) & s.notna(),
        "<":  lambda s: s < value,
        "<=": lambda s: s <= value,
        ">":  lambda s: s > value,
        ">=": lambda s: s >= value,
    }
    return ops[op](series)


def apply_filters(df: pd.DataFrame, filters) -> pd.DataFrame:
    """Apply the config's SQL filters to a frame, as the WHERE clause would."""
    if not filters:
        return df
    mask = None
    for pred in filters:
        one = _evaluate(_parse(pred), df)
        mask = one if mask is None else (mask & one)
    return df[mask]


# --- the source --------------------------------------------------------------
def _parse_ts(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


class ExtractSource:
    """One frozen extract directory, read through its manifest."""

    def __init__(self, root: Path):
        self.root = Path(root)
        manifest_path = self.root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise ExtractError(
                f"no {MANIFEST_NAME} in {self.root}. A directory only counts as"
                f" an extract once its manifest is there -- an extraction still"
                f" in progress has files and no manifest."
            )
        with manifest_path.open() as fh:
            self.manifest = json.load(fh)
        self.files = self.manifest.get("files") or {}
        if not self.files:
            raise ExtractError(f"{manifest_path} records no files")
        self.extract_hash = self.manifest.get("extract_hash")
        self.request = self.manifest.get("request") or {}
        self._verified: set[str] = set()
        self._verify = os.environ.get(ENV_VERIFY, "1") != "0"

    # -- provenance
    def lineage(self) -> dict:
        """What to record in a manifest so a result names the data it saw."""
        return {
            "extract_dir": str(self.root),
            "extract_hash": self.extract_hash,
            "request_hash": self.manifest.get("request_hash"),
            "watermark": self.manifest.get("watermark"),
            "files": {
                name: {"sha256": meta.get("sha256"), "rows": meta.get("rows")}
                for name, meta in sorted(self.files.items())
            },
        }

    # -- reading
    def _entry(self, name: str) -> dict:
        try:
            return self.files[name]
        except KeyError:
            raise ExtractError(
                f"the extract at {self.root} has no {name!r} dataset"
                f" (has: {', '.join(sorted(self.files))})"
            ) from None

    def _path(self, name: str) -> Path:
        entry = self._entry(name)
        path = self.root / entry["file"]
        if not path.is_file():
            raise ExtractError(
                f"{path} is named by the manifest but missing from the extract")
        if self._verify and name not in self._verified:
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != entry.get("sha256"):
                raise ExtractError(
                    f"{path} does not match the digest its manifest recorded"
                    f" ({actual[:12]} != {str(entry.get('sha256'))[:12]}). The"
                    f" extract is supposed to be immutable; something wrote to"
                    f" it, so nothing trained on it can be trusted."
                )
            self._verified.add(name)
        return path

    def window(self, name: str) -> dict[str, pd.Timestamp]:
        """The bindings actually used for one dataset, as recorded."""
        return {k: _parse_ts(v)
                for k, v in (self._entry(name).get("window") or {}).items()}

    def _covers(self, name: str, checks) -> None:
        """Refuse to answer from an extract that is a subset of the ask.

        `checks` is a list of `(bound_name, relation, asked)`. A subset extract
        is the dangerous case precisely because it succeeds: fewer rows, a
        trained model, a plausible number.
        """
        w = self.window(name)
        for bound, relation, asked in checks:
            if bound not in w:
                raise ExtractError(
                    f"{name}: the manifest records no {bound!r} bound, so"
                    f" whether it covers this cohort cannot be established")
            have = w[bound]
            asked = _parse_ts(asked) if not isinstance(asked, pd.Timestamp) \
                else asked.tz_convert("UTC")
            ok = have <= asked if relation == "<=" else have >= asked
            if not ok:
                raise ExtractError(
                    f"{name}: the extract's {bound} is {have.isoformat()}, but"
                    f" this cohort needs {bound} {relation} {asked.isoformat()}."
                    f" Training on it would silently train on a subset."
                    f" Extract a wider window."
                )

    def _read(self, name: str, *, columns=None, filters=None) -> pd.DataFrame:
        """One dataset, with column and row-group pushdown.

        Pushdown is not an optimisation here. `runs.parquet` spans the whole
        extract window, which is a superset of any one cohort's; reading it
        whole and then narrowing would put the widest frame in memory at the
        peak the loader was already OOM-killed at once (data_loader.py's
        categorical-downcast note, 2026-07-15).
        """
        return pd.read_parquet(self._path(name), columns=columns,
                               filters=filters or None)

    # -- the six datasets, one method each, mirroring one loader each
    def runs(self, *, train_start, as_of_date, target_column, keep_columns,
             filters) -> pd.DataFrame:
        """`data_loader.load`'s main query.

        `keep_columns` is what that SELECT lists; filter-only columns are read
        and then dropped, exactly as a WHERE clause uses a column it does not
        select.
        """
        self._covers("runs", [("train_start", "<=", train_start),
                              ("as_of_date", ">=", as_of_date)])
        needed = set(keep_columns) | set(filter_columns(filters)) \
            | {"pending_at", target_column}
        available = set(self._entry("runs").get("columns") or [])
        missing = sorted(needed - available) if available else []
        if missing:
            raise ExtractError(
                f"runs: the extract does not carry {missing}. Columns are fixed"
                f" by trusted code (host/extractor/inventory.py)."
            )
        df = self._read(
            "runs",
            columns=sorted(needed),
            filters=[("pending_at", ">=", train_start),
                     ("pending_at", "<", as_of_date)],
        )
        # Row-group pushdown is coarse -- a group is kept if ANY row in it can
        # match -- so the exact bounds still have to be applied.
        df = df[(df["pending_at"] >= train_start)
                & (df["pending_at"] < as_of_date)]
        df = apply_filters(df, filters)
        df = _parse_tags(df)
        df = df.rename(columns={target_column: "y"})
        keep = [c for c in list(keep_columns) if c != target_column]
        return df[keep + ["y"]].reset_index(drop=True)

    def worker_counts(self, fetch_from, fetch_to) -> pd.DataFrame:
        """`data_loader.load_worker_counts`."""
        self._covers("worker_counts", [("window_lower", "<=", fetch_from),
                                       ("as_of_date", ">=", fetch_to)])
        df = self._read("worker_counts",
                        filters=[("sampled_at", ">=", fetch_from),
                                 ("sampled_at", "<", fetch_to)])
        df = df[(df["sampled_at"] >= fetch_from) & (df["sampled_at"] < fetch_to)]
        return df.sort_values(["task_queue_id", "sampled_at"]) \
                 .reset_index(drop=True)

    def worker_pools(self) -> pd.DataFrame:
        """`data_loader.load_worker_pools`. Whole table, no window."""
        return self._read("worker_pools").reset_index(drop=True)

    def throughput_runs(self, window_start, window_end) -> pd.DataFrame:
        """`data_loader.load_task_runs_for_throughput`.

        The upper bound is INCLUSIVE, as that query's is.
        """
        self._covers("throughput_runs", [("window_lower", "<=", window_start),
                                         ("as_of_date", ">=", window_end)])
        df = self._read("throughput_runs",
                        filters=[("resolved_at", ">=", window_start),
                                 ("resolved_at", "<=", window_end)])
        df = df[df["resolved_at"].notna()
                & (df["resolved_at"] >= window_start)
                & (df["resolved_at"] <= window_end)
                & df["task_queue_id"].notna()]
        return df.reset_index(drop=True)

    def qctx_runs(self, window_start, as_of_date, ref_lower) -> pd.DataFrame:
        """`data_loader.load_task_runs_for_queue_context`.

        The overlap predicate is `exit IS NULL OR exit > window_start`, where
        `exit = COALESCE(started_at, resolved_at)`. A LOWER `window_lower` in
        the extract keeps MORE rows, so `<=` is the containment direction here
        too.

        REFUSED WHEN THE EXTRACT LACKS `task_created`. The SQL floors BOTH sides
        of its join -- `r.pending_at >= ref_lower` AND `t.task_created >=
        ref_lower`. The trainer's `ref_lower` is derived from `train_start - 90m`
        while the extract's hangs off the generic `train_start - 24h` prefix, so
        the two floors are not the same instant and the tasks-side one has to be
        re-applied here. Without the column it cannot be, and the result would be
        a SUPERSET: reference runs whose task was created before the floor, which
        the database cohort never saw, contributing to every queue-context count
        for the first rows of the window.

        An earlier version of this method skipped the floor with a comment
        claiming re-applying it "would only ever drop rows this query kept".
        That is true and is exactly the problem -- those rows are dropped by the
        query being mirrored, so keeping them is a different population, not a
        conservative one.

        `task_created` is carried by `DATASETS["qctx_runs"]` since 2026-08-30 and
        is dropped from the returned frame, which the DB path never selects: the
        column exists to be filtered on, not to reach the feature code. An
        extract published before that date lacks it, and `request_hash` does not
        cover the column list -- so re-extracting needs a BUMPED `generation` or
        D20 hands back the old artifact and this raises again.
        """
        available = set(self._entry("qctx_runs").get("columns") or [])
        if "task_created" not in available:
            raise ExtractError(
                "qctx_runs: this extract does not carry `task_created`, so the"
                " tasks-side floor `t.task_created >= ref_lower` cannot be"
                " re-applied and the reference set would be a SUPERSET of what"
                " the SQL cohort saw -- a different population, silently."
                " Add the column to host/extractor/inventory.py and re-extract"
                " with a bumped `generation`, or run a config without"
                " queue_context_features."
            )
        self._covers("qctx_runs", [("ref_lower", "<=", ref_lower),
                                   ("as_of_date", ">=", as_of_date),
                                   ("window_lower", "<=", window_start)])
        df = self._read("qctx_runs",
                        filters=[("pending_at", ">=", ref_lower),
                                 ("pending_at", "<", as_of_date)])
        exit_at = df["started_at"].fillna(df["resolved_at"])
        df = df[(df["pending_at"] >= ref_lower)
                & (df["pending_at"] < as_of_date)
                & (exit_at.isna() | (exit_at > window_start))
                & (df["task_created"] >= ref_lower)
                & df["task_queue_id"].notna()]
        return df.drop(columns=["task_created"]).reset_index(drop=True)

    def anomalous_dates(self, flag_subset) -> set[datetime.date]:
        """`data_loader.load_anomalous_dates`.

        The extract carries the rows and every flag; the config-driven
        disjunction that was a WHERE clause is done here, which is where
        `inventory.py` says it belongs.
        """
        df = self._read("daily_health")
        if flag_subset:
            mask = None
            for flag in flag_subset:
                if flag not in df.columns:
                    raise ExtractError(
                        f"daily_health has no column {flag!r}"
                        f" (has: {', '.join(sorted(df.columns))})")
                col = df[flag].fillna(False).astype(bool)
                mask = col if mask is None else (mask | col)
        else:
            mask = df["is_anomalous"].fillna(False).astype(bool)
        return {_as_date(v) for v in df.loc[mask, "sample_date"]}


def _as_date(value) -> datetime.date:
    """`date32` reads back as `datetime.date`; a timestamp column would not.

    The DB path returns `datetime.date` from psycopg, and the caller compares
    against `pending_at.dt.date`, so anything else here would silently never
    match and the anomaly filter would exclude nothing.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return pd.Timestamp(value).date()


def _parse_tags(df: pd.DataFrame) -> pd.DataFrame:
    """`tags` is JSONB over psycopg (a dict) and `json_text` in the extract.

    THIS IS NOT COSMETIC. `FeatureBuilder._extract_tags` returns `None` for any
    non-dict, so a `tags` column left as a JSON string yields all-null tag
    features -- for `run_duration_residual`, nine of them -- with no error
    anywhere. The extract writes a string on purpose (Arrow inference silently
    drops keys absent from the first batch); parsing it back is this side's job.
    """
    if "tags" not in df.columns:
        return df

    def parse(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    df = df.copy()
    df["tags"] = df["tags"].map(parse)
    return df
