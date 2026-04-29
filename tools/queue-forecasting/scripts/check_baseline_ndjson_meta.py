"""Validate a baseline-predictions NDJSON sidecar against requested coverage.

Exit codes:
  0  metadata is valid for the requested coverage; reuse the file
  1  metadata is missing, malformed, or insufficient; caller should regenerate

Stdout: short reason string suitable for logging.

Usage:
  python check_baseline_ndjson_meta.py <meta_path> <from> <to> <exclude_csv>

  <meta_path>     path to the .meta.json sidecar (may not exist)
  <from>          requested start date (YYYY-MM-DD), inclusive
  <to>            requested end date (YYYY-MM-DD), exclusive
  <exclude_csv>   comma-separated list of dates excluded from the baseline's
                  trailing history; empty string = no exclusions
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def parse_excludes(csv: str) -> set[str]:
    return {x.strip() for x in csv.split(",") if x.strip()}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(f"usage: {Path(__file__).name} <meta_path> <from> <to> <exclude_csv>", file=sys.stderr)
        return 1
    meta_path = Path(argv[0])
    requested_from = parse_date(argv[1])
    requested_to = parse_date(argv[2])
    requested_excludes = parse_excludes(argv[3])

    if not meta_path.exists():
        print("no metadata sidecar — regenerating")
        return 1

    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"meta sidecar unreadable ({exc}) — regenerating")
        return 1

    try:
        existing_from = parse_date(meta["from_date"])
        existing_to = parse_date(meta["to_date"])
        existing_excludes = set(meta.get("exclude_dates", []))
    except (KeyError, ValueError) as exc:
        print(f"meta sidecar malformed ({exc}) — regenerating")
        return 1

    if existing_excludes != requested_excludes:
        added = requested_excludes - existing_excludes
        removed = existing_excludes - requested_excludes
        diff = []
        if added:
            diff.append(f"+{sorted(added)}")
        if removed:
            diff.append(f"-{sorted(removed)}")
        print(f"exclude_dates mismatch ({' '.join(diff)}) — regenerating")
        return 1

    if existing_from > requested_from:
        print(f"existing from {existing_from} > requested {requested_from} — regenerating (need earlier history)")
        return 1

    if existing_to < requested_to:
        print(f"existing to {existing_to} < requested {requested_to} — regenerating (need later coverage)")
        return 1

    print(
        f"valid: existing=[{existing_from}..{existing_to}) "
        f"covers requested=[{requested_from}..{requested_to}) "
        f"with {len(existing_excludes)} exclusion(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
