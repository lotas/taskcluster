"""The Parquet side of the seam. Task 3.

**NOT COVERED BY THE TEST SUITE IN THIS REPOSITORY, AND SAID SO PLAINLY.**
`pyarrow` is not importable in the development environment, so nothing here has
executed. It first runs in the privileged tasks.

NO TYPE INFERENCE ANYWHERE, and that is the whole design of this file. An earlier
version built the schema from the first batch, which is a schema derived from an
arbitrary subset of the data and fails in two ways that both look like something
else:

  * A column that is all-NULL in the first batch infers as `null`, and the first
    real value in a later batch raises `ArrowNotImplementedError`. `started_at`
    is NULL for every still-pending run, so this is not a corner case.
  * `tags` is JSONB and psycopg returns a dict. Inference builds a STRUCT from
    the keys in the first batch and SILENTLY DROPS keys that first appear later:
    `{"retries": "2"}` was observed becoming `{"kind": null}`. A dropped tag is a
    feature a candidate cannot see and cannot know it cannot see.

So the schema comes from `inventory.DATASETS[...].types`, which is read off the
live DDL, and `tags` is written as `json_text` -- the raw JSON as a string, which
preserves arbitrary keys by construction and is what the trainer already parses
`tags.*` features out of.

FILE BYTES ARE NOT REPRODUCIBLE ACROSS EXTRACTIONS. None of `inventory`'s queries
carries an `ORDER BY`, so the same rows may come back in a different order.
Nothing depends on byte-reproducibility: NC18's byte-identical reuse holds
because the bytes are not regenerated (D20 publishes once), and adding `ORDER BY`
over a months-long window invites the external sort D23 exists to bound. The
claim is withdrawn rather than implemented, and this note exists so nobody
re-adds the sort to restore a property nothing needs.
"""
from __future__ import annotations

import json

import pyarrow
import pyarrow.parquet

# Fixed rather than file-sized: a file-sized row group means the writer holds the
# whole dataset before flushing, which is the materialisation the streaming seam
# exists to avoid.
ROW_GROUP_ROWS = 10_000

# `inventory`'s type vocabulary -> Arrow. Timestamps are microsecond UTC because
# that is what psycopg returns for TIMESTAMPTZ and what Parquet stores natively;
# leaving the timezone off would turn an instant into a local reading of one.
_ARROW = {
    "string": lambda: pyarrow.string(),
    "int32": lambda: pyarrow.int32(),
    "float64": lambda: pyarrow.float64(),
    "timestamp": lambda: pyarrow.timestamp("us", tz="UTC"),
    "date": lambda: pyarrow.date32(),
    "bool": lambda: pyarrow.bool_(),
    # JSONB arrives as a dict and is stored as its JSON text.
    "json_text": lambda: pyarrow.string(),
}


def schema_for(columns, types):
    """The Arrow schema for one dataset, from declared types only.

    Every field is NULLABLE. Some source columns are `NOT NULL` in the DDL, but a
    schema that declared them non-nullable would turn a single unexpected NULL
    into a hard failure mid-extract -- and the dataset's own emptiness check is
    the control that catches a broken read, not a nullability constraint.
    """
    missing = [c for c in columns if c not in types]
    if missing:
        raise ValueError(f"no declared type for {missing}")
    return pyarrow.schema([
        pyarrow.field(name, _ARROW[types[name]](), nullable=True)
        for name in columns
    ])


def _as_json_text(value):
    """A JSONB value as canonical JSON text, or None.

    `sort_keys` so the same tags always serialise the same way -- not for
    determinism across extractions, which is not claimed, but so a candidate
    diffing two rows sees a difference only when the tags differ. psycopg may
    hand back a dict, a list, or a string depending on how the column is
    configured; all three are accepted and only a string is passed through.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ParquetWriter:
    """The `writer` the extractor takes: an object with `.open(path, columns)`.

    A class rather than a module-level `open()`, because a module whose public
    name is `open` shadows the builtin for everything else in the file.
    """

    def __init__(self, types_for):
        # `types_for(name_or_columns)` is injected rather than importing
        # `inventory` here, so this module's dependency list stays `pyarrow` and
        # the extractor decides which dataset it is writing.
        self.types_for = types_for

    def open(self, path, columns):
        columns = list(columns)
        return _Sink(path, columns, self.types_for(columns))


class _Sink:
    def __init__(self, path, columns, types):
        self.path = path
        self.columns = columns
        self.types = types
        self.schema = schema_for(columns, types)
        self._json_columns = [i for i, c in enumerate(columns)
                              if types[c] == "json_text"]
        self._writer = pyarrow.parquet.ParquetWriter(
            path, self.schema,
            compression="zstd", compression_level=3, version="2.6",
            write_statistics=True)

    def write(self, batch):
        """One batch of row tuples, straight from the cursor.

        The schema is FIXED before the first batch arrives, so a batch whose
        values do not fit fails here rather than silently establishing a
        different layout for the rest of the file.
        """
        data = {}
        for i, name in enumerate(self.columns):
            if i in self._json_columns:
                data[name] = [_as_json_text(row[i]) for row in batch]
            else:
                data[name] = [row[i] for row in batch]
        table = pyarrow.table(data, schema=self.schema)
        self._writer.write_table(table, row_group_size=ROW_GROUP_ROWS)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Closed on every path. An unclosed ParquetWriter leaves a file with no
        # footer, which reads as a corrupt Parquet file rather than as an
        # interrupted extraction -- and the staging directory is removed on
        # failure anyway, so the only thing an unclosed writer costs is a
        # misleading error.
        self._writer.close()
        return False
