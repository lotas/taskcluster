#!/usr/bin/env python3
"""Join `qf list` and `qf status` into one row per scored experiment.

Reads the two payloads on stdin (see `results.sh`, which is what fetches them);
prints a table, or the joined rows with `--json`. A separate file rather than a
heredoc inside the shell script because the formatting needs quotes of both
kinds, and a `python3 -c '...'` body that cannot contain a single quote is a body
nobody edits safely.

THE COLUMN THAT MATTERS MOST IS `extract`. A row against a different extract is
not a better or worse result, it is a result from another series -- and two of
them side by side in one table is exactly how a regime change gets read as a
model improvement. Hence the prefix on every row and the warning at the bottom.
"""
from __future__ import annotations

import json
import sys

SEP_PAYLOAD = "\x1d"     # between the job list and the statuses
SEP_STATUS = "\x1e"      # between statuses


def _loads(text, default):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def build(jobs_raw, statuses_raw):
    jobs = {j["run_id"]: j
            for j in (_loads(jobs_raw, {}).get("jobs") or [])
            if j.get("run_id")}
    rows = []
    for chunk in statuses_raw.split(SEP_STATUS):
        chunk = chunk.strip()
        if not chunk:
            continue
        job = (_loads(chunk, {}) or {}).get("job")
        if not isinstance(job, dict) or not job.get("run_id"):
            continue
        pins = job.get("pins") or {}
        spec = _loads(jobs.get(job["run_id"], {}).get("spec_json") or "{}", {})
        board = _loads(pins.get("scoreboard") or "{}", {}).get("metrics") or {}
        probe = pins.get("judged_run") or (spec.get("args") or {}).get("run", "")
        # THE PROBE'S NOTE, not the evaluation's. An evaluation can be re-run
        # against the same probe under a different contract, and what the
        # EXPERIMENT was is a fact about the training run.
        probe_spec = _loads(jobs.get(probe, {}).get("spec_json") or "{}", {})
        rows.append({
            "evaluation": job["run_id"],
            "probe": probe,
            "when": (job.get("submitted_at") or "")[:16].replace("T", " "),
            "verdict": pins.get("verdict") or "-",
            "note": probe_spec.get("note") or spec.get("note") or "",
            "extract": pins.get("request_hash") or "",
            "baseline": pins.get("baseline_hash") or "",
            "contract": pins.get("contract_hash") or "",
            "metrics": {n: m.get("measured") for n, m in board.items()
                        if isinstance(m, dict)},
            "passed": {n: m.get("passed") for n, m in board.items()
                       if isinstance(m, dict)},
        })
    return rows


def _split_note(note):
    """`cfg=<path> | <free text>` -> (config label, free text).

    `first-probe.sh` writes that shape. Anything else goes through whole, in the
    free-text column, because a note a human typed by hand is still the best
    description of the run.
    """
    text = note or ""
    if not text.startswith("cfg="):
        return "", text
    head, _, rest = text.partition(" | ")
    label = head[len("cfg="):]
    label = label.rsplit("/", 1)[-1]
    if label.endswith(".yaml"):
        label = label[: -len(".yaml")]
    return label, rest


def _elide(text, width):
    """Keep the TAIL when a config name is too long.

    The names in this project are `wait_time_residual_throughput_filtered_
    baseline` and `..._baseline_qctx`, which differ in their last six
    characters -- so truncating from the right prints two different experiments
    under the same label, which is the one thing a results table must never do.
    """
    if len(text) <= width:
        return text
    return "\u2026" + text[-(width - 1):]


def render(rows):
    out = []
    # Metric columns from the UNION, in a stable order, so a run scored by a
    # contract with one extra metric does not shift every other row's columns.
    names = sorted({n for r in rows for n in r["metrics"]})
    widths = {n: max(12, len(n)) for n in names}
    # NOT written back onto the rows: `--json` returns the same list, and a
    # display-only field appearing there depending on whether something rendered
    # first is a difference between two callers of the same function.
    split = {r["evaluation"]: _split_note(r["note"]) for r in rows}
    cfg_w = min(40, max(6, *(len(c) for c, _ in split.values())))

    def line(when, verdict, extract, config, cells, free):
        return ("{:<16}  {:<7}  {:<8}  ".format(when, verdict, extract)
                + "  ".join(cells) + "  {:<{}}  {}".format(config, cfg_w, free))

    head = line("when", "verdict", "extract", "config",
                ["{:>{}}".format(n, widths[n]) for n in names], "note")
    out.append(head)
    out.append("-" * len(head.rstrip()))
    for r in rows:
        cells = []
        for name in names:
            value = r["metrics"].get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                # A trailing `!` on a metric that missed its bar. The bars are
                # in the contract and identical for every row in a series, so
                # printing them would be the same numbers on every line.
                mark = "" if r["passed"].get(name) is True else "!"
                cells.append("{:>{}.4g}{:<1}".format(
                    value, widths[name] - 1, mark))
            else:
                cells.append("{:>{}}".format("-", widths[name]))
        config, free = split[r["evaluation"]]
        out.append(line(r["when"], r["verdict"], r["extract"][:8],
                        _elide(config, cfg_w), cells, free[:40]))

    out.append("")
    out.append("`!` = missed its bar.  Full numbers: ./results.sh --json")

    series = {(r["extract"][:8], r["baseline"][:8], r["contract"][:8])
              for r in rows}
    if len(series) > 1:
        out.append("")
        out.append(f"WARNING: {len(series)} different input sets in this table."
                   " Rows from different extracts, baselines or contracts are"
                   " NOT comparable:")
        for key in sorted(series):
            n = sum(1 for r in rows
                    if (r["extract"][:8], r["baseline"][:8],
                        r["contract"][:8]) == key)
            out.append(f"  {n:>3} row(s)  extract={key[0]}  baseline={key[1]}"
                       f"  contract={key[2]}")
    return "\n".join(out)


def main(argv):
    raw = sys.stdin.read()
    jobs_raw, _, statuses_raw = raw.partition(SEP_PAYLOAD)
    rows = build(jobs_raw, statuses_raw)
    if not rows:
        print("no scored experiments found.")
        print("  (a probe alone is not a result: a contract has to score it)")
        return 0
    if "--json" in argv:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
