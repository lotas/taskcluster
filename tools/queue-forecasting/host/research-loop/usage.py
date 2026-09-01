#!/usr/bin/env python3
"""Extract one agent call's usage and append a compact, human-readable line."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def _number(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def _compact(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _claude(
    raw: str,
) -> tuple[str, int | None, float | None, dict[str, int], list[str]]:
    doc = json.loads(raw)
    usage = doc.get("usage") or {}
    parts = {
        "input": _number(usage.get("input_tokens")),
        "cache_write": _number(usage.get("cache_creation_input_tokens")),
        "cache_read": _number(usage.get("cache_read_input_tokens")),
        "output": _number(usage.get("output_tokens")),
    }
    total = sum(parts.values()) if usage else None
    cost = doc.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = None
    return str(doc.get("result") or ""), total, cost, parts, []


def _codex_cost(parts: dict[str, int]) -> tuple[float | None, str | None]:
    names = (
        "QF_CODEX_INPUT_USD_PER_MTOK",
        "QF_CODEX_CACHED_INPUT_USD_PER_MTOK",
        "QF_CODEX_OUTPUT_USD_PER_MTOK",
    )
    raw = [os.environ.get(name, "").strip() for name in names]
    missing = sum(not value for value in raw)
    if missing == len(raw):
        return None, "rates=unset"
    if missing:
        return None, "rates=partial"
    try:
        rates = [float(value) for value in raw]
    except ValueError:
        return None, "rates=invalid"
    if any(not math.isfinite(rate) or rate < 0 for rate in rates):
        return None, "rates=invalid"

    input_rate, cached_rate, output_rate = rates
    uncached = max(0, parts["input"] - parts["cached"])
    cost = (
        uncached * input_rate
        + parts["cached"] * cached_rate
        + parts["output"] * output_rate
    ) / 1_000_000
    return cost, None


def _codex(
    raw: str,
) -> tuple[str, int | None, float | None, dict[str, int], list[str]]:
    answer = ""
    usage: dict[str, object] | None = None
    skipped = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(event, dict):
            skipped += 1
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = str(item.get("text") or "")
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}

    notes = [f"skipped_lines={skipped}"] if skipped else []
    if usage is None:
        return answer, None, None, {}, notes

    parts = {
        "input": _number(usage.get("input_tokens")),
        "cached": _number(usage.get("cached_input_tokens")),
        "output": _number(usage.get("output_tokens")),
        "reasoning": _number(usage.get("reasoning_output_tokens")),
    }
    # Codex reports cached input as a subset of input, and reasoning as a
    # subset of output. Do not double-count either in the displayed total.
    total = parts["input"] + parts["output"]

    cost, rate_note = _codex_cost(parts)
    if rate_note:
        notes.append(rate_note)
    return answer, total, cost, parts, notes


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in {"claude", "codex"}:
        print(
            "usage: usage.py {claude|codex} RAW_FILE USAGE_LOG EXIT_CODE",
            file=sys.stderr,
        )
        return 2

    provider, raw_path, log_path, exit_code = sys.argv[1:]
    raw = Path(raw_path).read_text(errors="replace")
    try:
        answer, total, cost, parts, notes = (
            _claude(raw) if provider == "claude" else _codex(raw)
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        # Preserve unexpected output for the caller. Logging "unknown" makes
        # format drift visible without turning accounting into an execution gate.
        answer, total, cost, parts, notes = raw, None, None, {}, ["parse=failed"]

    if answer:
        print(answer)

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    estimate = f"~${cost:.4f}" if cost is not None else "n/a"
    exact = {"total": total, **parts} if total is not None else parts
    detail = " ".join(f"{key}={value}" for key, value in exact.items())
    suffix = f" [{detail}]" if detail else ""
    suffix += "".join(f" [{note}]" for note in notes)
    suffix += f" [exit={exit_code}]"
    line = (
        f"{stamp}  {provider:<7}  {_compact(total)} tokens  "
        f"est {estimate}{suffix}\n"
    )
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError as exc:
        print(f"cannot append usage to {log_path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
