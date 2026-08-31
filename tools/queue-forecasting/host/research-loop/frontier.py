#!/usr/bin/env python3
"""The progress artifact: what the loop has established, per series.

WHY THIS EXISTS. `results.sh` prints every scored run, oldest first, and that is
the right primitive and the wrong thing to read once there are eighty of them.
The questions an operator actually has are not per-row:

    is the frontier moving, or is the loop re-measuring the same thing?
    which config is the best on each bar, and in WHICH series?
    did the agent's own predictions come true, or is it narrating after the fact?
    what is one cohort away from being believable?

None of that needs new statistics. It needs the rows grouped by series and read
against the pre-registrations that were written before them.

WHY SERIES AND NOT TIME. `results.py` already warns when one table holds rows
from two input sets; this makes that structural. A series is
`(extract, baseline, contract)`, and a "best so far" computed across series is
the exact mistake the whole project has made twice -- Phase 3a's regime drift and
Finding 2's 3.6x cross-series comparison were both this. So the frontier is
per-series and there is no global best.

THE CONFIRM GATE. A config that clears every bar in one series is PROMISING, not
CONFIRMED. Confirmation needs a second series whose HOLDOUT WINDOW DOES NOT
OVERLAP the first -- computed from the extracts' `as_of_date` and the contract's
`holdout_days`, not assumed from the hashes being different. Two extracts one day
apart share four fifths of their holdout and are one result, not two.

This is deliberately cheaper than the design's Phase 3 (moving-block bootstrap,
BH-FDR, disjoint-day decomposition). It buys the one property that matters for an
unattended loop -- a win has to repeat on data it was not selected on -- and it
buys it with arithmetic instead of a framework.

    results.sh --json | frontier.py --journal <dir>          the report
    results.sh --json | frontier.py --journal <dir> --json   machine-readable

`--journal` is what makes a result "already written up". Without it every row
reads as unrecorded, which is loud rather than wrong -- the opposite default
would quietly retire results nobody ever wrote about.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# One directory up is `host/`, which holds `experiment.py`. Imported rather than
# reimplemented: `qf` and `parse_day` are already written and already tested
# there, and a second `qf` helper here would be a second thing to keep in step
# with the client's exit conventions.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prereg                                                  # noqa: E402
import experiment                                              # noqa: E402

# MIRRORS the contract's `direction` per metric, used ONLY to rank two measured
# values against each other. Pass/fail is never computed here -- the scoreboard's
# `passed` comes from the root-owned evaluator, and recomputing it in agent-side
# code would create a second answer to a question that already has an
# authoritative one. `band` metrics have no "better", only "inside".
RANK = {"mae": "lower", "p90_miss_tail": "lower", "within_2x": "higher",
        "p90_coverage": "band"}


def load_contract(contracts, contract_hash):
    """The contract body, for `holdout_days` and its metric directions.

    Read off disk rather than over `qf`: `_op_contracts` returns only the hash,
    the filename and the directory, because the file is root-owned in the trusted
    checkout and qfd is not its reader. Read-only and non-authoritative -- if
    this fails the report degrades to "overlap unknown" rather than refusing.
    """
    for row in contracts.get("contracts") or []:
        if row.get("contract_hash") != contract_hash:
            continue
        path = os.path.join(contracts.get("dir") or "", row.get("file") or "")
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


def _band_bounds(contract):
    """`{metric: (low, high)}` for every band metric the contract defines.

    Read from the contract rather than hardcoded: the band is
    `wait_time.v1.json`'s `p90_coverage.bar.{low,high}` today, and a metric whose
    bounds moved would otherwise be judged against the old ones.
    """
    out = {}
    for name, spec in ((contract.get("metrics") or {}).items()):
        bar = (spec or {}).get("bar") or {}
        if bar.get("kind") != "band":
            continue
        low, high = bar.get("low"), bar.get("high")
        if isinstance(low, (int, float)) or isinstance(high, (int, float)):
            out[name] = (low, high)
    return out


def holdout_window(extract, contract):
    """`(first_day, last_day)` of the holdout this series scores on.

    The cohort's holdout is the `holdout_days` immediately before `as_of`, which
    is how `run_cohort.py` derives every other window boundary. Returns `None`
    when either input is unreadable, and callers must treat that as "cannot
    prove non-overlap" rather than as "does not overlap".
    """
    as_of = experiment.parse_day(extract.get("as_of_date"))
    days = contract.get("holdout_days")
    if as_of is None or not isinstance(days, int) or days <= 0:
        return None
    return (as_of - datetime.timedelta(days=days), as_of)


def windows_overlap(left, right):
    """Half-open `[first, last)` overlap. `None` on either side means unknown,
    and unknown is reported as OVERLAPPING -- the conservative direction, because
    the failure this gate prevents is confirming on a cohort that was really the
    same cohort."""
    if left is None or right is None:
        return True
    return left[0] < right[1] and right[0] < left[1]


# A run id is `<kind>-<stamp>-<sha>-<seq>` (`qfd.py:1277`). Matched loosely on
# purpose: the journal is prose, and an id inside a sentence, a table cell or a
# code fence must all count.
_RUN_ID = re.compile(r"\b(?:probe|evaluate)-[0-9A-Za-z]+-[0-9a-f]+-\d+\b")


def _tracked_journal_files(journal_dir):
    """Basenames of the git-tracked `.md` files directly in `journal_dir`.

    `None` means the question could not be answered -- no repository, no git, or
    a failed call -- and every caller must treat that as "nothing is tracked"
    rather than as "everything is".
    """
    import subprocess
    try:
        done = subprocess.run(
            ["git", "-C", journal_dir, "ls-files", "-z", "--", "."],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out = set()
    for raw in done.stdout.split(b"\0"):
        path = raw.decode("utf-8", "replace")
        # `ls-files -- .` from inside the directory prints paths relative to it,
        # so a bare name is a file directly here and anything with a slash is in
        # a subdirectory (`escalations/`), which does not count.
        if path and "/" not in path:
            out.add(path)
    return out


def journaled_run_ids(journal_dir):
    """Every run id cited by a RECORDED journal entry.

    WHY THIS IS READ AT ALL. Without it the loop cannot tell a new result from one
    it wrote up an hour ago, so the first action in `tick-prompt.md` ("a finished
    run is unrecorded") matches forever and every tick re-narrates the same row.
    A refuted claim has the same problem: `broken` is a permanent state, so
    "write what this rules out" would also never be done.

    ESCALATIONS ARE DELIBERATELY EXCLUDED. An escalated entry was NOT recorded --
    the copilot rejected it -- so the run it describes is still unwritten and must
    come round again. Counting escalations here would let a rejected claim
    silently retire the result it was about, which is the worst possible reading
    of a failed verification.
    """
    seen = set()
    try:
        names = sorted(os.listdir(journal_dir))
    except OSError:
        return seen

    # ONLY TRACKED FILES COUNT. `os.listdir` alone read the working tree, and the
    # leader shares the uid that owns it -- so dropping a `journal/anything.md`
    # citing a run id marked that run "written up" without any entry ever being
    # verified or committed. Retiring a result is exactly what an unverified file
    # must not be able to do.
    tracked = _tracked_journal_files(journal_dir)
    if tracked is None:
        # Cannot tell tracked from untracked: count NOTHING. Every run then reads
        # as unrecorded, which is noisy and safe; the opposite default would
        # silently retire results on the strength of files nobody reviewed.
        return seen

    for name in names:
        if not name.endswith(".md") or name == "PENDING.md":
            continue
        if name not in tracked:
            continue
        try:
            with open(os.path.join(journal_dir, name),
                      encoding="utf-8", errors="replace") as fh:
                seen.update(_RUN_ID.findall(fh.read()))
        except OSError:
            continue
    return seen


def series_key(row):
    return (row.get("extract", ""), row.get("baseline", ""),
            row.get("contract", ""))


def better(name, value, incumbent):
    """Is `value` a better measurement of `name` than `incumbent`?"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if incumbent is None:
        return True
    how = RANK.get(name)
    if how == "lower":
        return value < incumbent
    if how == "higher":
        return value > incumbent
    return False        # `band`: no ordering, so the first measurement stands


def _band_distance(value, band):
    """How far outside `(low, high)` a value sits. Zero when inside."""
    low, high = band
    if low is not None and value < low:
        return low - value
    if high is not None and value > high:
        return value - high
    return 0.0


def judge_claim(row, index, band=None):
    """Did the pre-registered claim come true? Judged against `vs`, not the bar.

    AGAINST `vs` AND NOT AGAINST THE CONTRACT, because that is the discipline the
    experiment queue already runs on: "every entry is judged against entry 1
    rather than against the bar, so a config that beats the reference is an
    improvement whether or not the reference passes." Judging `improve` as "this
    bar passed" would mark a config that cut MAE by 12% as a broken claim, which
    is not what anyone claimed or believes.

    NO INVENTED TOLERANCE ANYWHERE. `improve` is a strict comparison of measured
    values, and `hold` is a comparison of PASS/FAIL STATUS -- which the contract
    defines, so it needs no threshold of ours. That matters because `hold` is a
    live claim on a bar that is currently FAILING (the 30m+ tail): "this change
    does not cost the tail" must be judgeable while the tail still misses, and a
    pass/fail reading alone would call that broken forever.

    A claim whose reference cannot be resolved is `unjudgeable`, never `kept`.
    """
    reg = row["prereg"]
    if not reg["registered"]:
        return "unregistered"
    bar = reg["bar"]
    mine = (row.get("metrics") or {}).get(bar)
    if not isinstance(mine, (int, float)) or isinstance(mine, bool):
        return "unmeasured"
    if reg.get("reference"):
        # DECLARED as the first run of a series, so there is nothing to judge it
        # against by construction. Reported as its own outcome rather than as
        # `unjudgeable`, which is a defect, or `kept`, which would be a claim.
        return "reference"
    if not reg["vs"]:
        return "unjudgeable: no vs"
    candidates = index.get(reg["vs"]) or []
    if not candidates:
        return "unjudgeable: vs not scored"
    # SAME SERIES PREFERRED, because that is the only comparison that means
    # anything -- and an id that also exists in another series must not make this
    # read as cross-series when a valid same-series row exists.
    same = [c for c in candidates if series_key(c) == series_key(row)]
    if len(same) > 1:
        return "unjudgeable: vs is ambiguous in this series"
    ref = same[0] if same else candidates[0]
    if series_key(ref) != series_key(row):
        # THE ONE COMPARISON THIS FILE EXISTS TO REFUSE. A reference from another
        # extract, baseline or contract is a different population, and a claim
        # judged across that boundary is Finding 2 all over again.
        return "unjudgeable: vs is another series"
    theirs = (ref.get("metrics") or {}).get(bar)
    if not isinstance(theirs, (int, float)) or isinstance(theirs, bool):
        return "unjudgeable: vs has no such metric"

    how = RANK.get(bar)

    if reg["direction"] == "hold":
        # NUMERIC, not pass/fail-status. Status equality was wrong in both
        # directions: a tail miss going 0.304 -> 0.900 would have read as "kept"
        # because both fail the 0.30 bar, and a fail -> pass improvement would
        # have read as "broken". `hold` means "did not get worse", so it is a
        # comparison of values with the tolerance the run pre-registered.
        #
        # The tolerance is in the immutable note, so the slack was claimed before
        # the number existed. Default 0 means strictly not worse.
        if how == "band":
            # A BAND STILL HAS A "WORSE": distance to the nearest edge. Reading it
            # as pass/fail alone repeated the very bug this direction was rewritten
            # to fix -- with both rows outside the band, coverage collapsing from
            # 0.84 to 0.01 counted as "kept" because neither passed.
            mine_ok = (row.get("passed") or {}).get(bar)
            ref_ok = (ref.get("passed") or {}).get(bar)
            if mine_ok is None or ref_ok is None:
                return "unjudgeable: no pass flag"
            if ref_ok and not mine_ok:
                return "broken"          # left the band
            if mine_ok:
                return "kept"            # inside it, which is all `hold` claims
            # Both outside. Compare how far outside, which needs the band's
            # edges; without them there is no ordering and the claim is not
            # judgeable rather than automatically kept.
            if band is None:
                return "unjudgeable: both outside the band, bounds unknown"
            return ("kept" if _band_distance(mine, band)
                    <= _band_distance(theirs, band) + reg["tol"]
                    else "broken")
        if how == "lower":
            return "kept" if mine <= theirs + reg["tol"] else "broken"
        return "kept" if mine >= theirs - reg["tol"] else "broken"

    if how == "band":
        # No ordering inside a band, so the only legible improvement is having
        # entered it.
        mine_ok = (row.get("passed") or {}).get(bar)
        ref_ok = (ref.get("passed") or {}).get(bar)
        if mine_ok is None or ref_ok is None:
            return "unjudgeable: no pass flag"
        return "kept" if (mine_ok and not ref_ok) else "broken"
    if how == "lower":
        return "kept" if mine < theirs else "broken"
    return "kept" if mine > theirs else "broken"


def build(rows, extracts, contracts, journaled=()):
    """Group into series, then roll configs up across them."""
    by_hash = {e.get("request_hash"): e for e in extracts.get("extracts") or []}

    series = {}
    for row in rows:
        key = series_key(row)
        entry = series.setdefault(key, {
            "extract": key[0], "baseline": key[1], "contract": key[2],
            "rows": [], "frontier": {}, "as_of": "", "holdout": None})
        entry["rows"].append(row)

    # Decoded first, ACROSS every series, because a pre-registration's `vs=` may
    # name a row this loop has not reached yet -- and a comparison that resolves
    # only when the reference happens to be earlier in the list is a comparison
    # that works by luck.
    # ID -> LIST OF ROWS, not id -> row. The same probe can be evaluated under
    # two contracts, and a single-valued index let the later evaluation overwrite
    # the earlier -- so a same-series claim citing that probe resolved to the
    # OTHER series and was refused as cross-series. Resolution below picks the
    # candidate in the citing row's own series.
    index = {}
    for row in rows:
        row["prereg"] = prereg.decode(row.get("note"))
        row["config_label"] = row["prereg"]["config"] or "(unlabelled)"
        # IDENTITY IS (path, content digest), NEVER the path alone. The agent
        # owns the checkout the path points into, so nothing stops it editing
        # `configs/x.yaml` between two cohorts -- and the second cohort exists
        # precisely to check the first, so one label over two different files is
        # the exact shape of a false CONFIRMED.
        row["config_id"] = (row["config_label"], row["prereg"]["cfgh"])
        row["recorded"] = any(i in journaled
                              for i in (row.get("evaluation"), row.get("probe"))
                              if i)
        for ident in (row.get("evaluation"), row.get("probe")):
            if ident:
                index.setdefault(ident, []).append(row)

    for key, entry in series.items():
        contract = load_contract(contracts, entry["contract"])
        extract = by_hash.get(entry["extract"], {})
        entry["as_of"] = extract.get("as_of_date") or ""
        entry["holdout"] = holdout_window(extract, contract)
        bands = _band_bounds(contract)
        for row in entry["rows"]:
            row["claim"] = judge_claim(row, index,
                                       band=bands.get(row["prereg"]["bar"]))
            for name, value in (row.get("metrics") or {}).items():
                slot = entry["frontier"].get(name)
                if better(name, value, (slot or {}).get("value")):
                    entry["frontier"][name] = {
                        "value": value, "config": row["config_label"],
                        "evaluation": row.get("evaluation", ""),
                        "passed": row.get("passed", {}).get(name)}

    # A config "cleared" a series when every metric the scoreboard reported
    # passed. `all()` over an EMPTY dict is True, so a run with no metrics is
    # excluded explicitly -- an unscored row must never read as a clean sweep.
    # KEYED BY BASELINE AND CONTRACT AS WELL AS THE CONFIG, so that only the
    # COHORT varies across a confirmation. Grouping by config alone let the same
    # config clear contract A on one extract and contract B on another and call
    # that two independent cohorts -- but a different contract is a different
    # question and a different baseline is a different thing to have beaten, so
    # neither pair is a repeat of the other. The extract is the only input a
    # confirmation is allowed to change.
    cleared = {}
    for key, entry in series.items():
        _extract, baseline, contract = key
        for row in entry["rows"]:
            flags = row.get("passed") or {}
            if flags and all(v is True for v in flags.values()):
                group = (row["config_id"], baseline, contract)
                cleared.setdefault(group, []).append(key)

    configs = {}
    for ((label, cfgh), baseline, contract), keys in cleared.items():
        windows = [series[k]["holdout"] for k in keys]
        independent = _independent_count(windows)
        # NO DIGEST, NO CONFIRMATION. A row from before digests existed cannot be
        # shown to be the same file as the row confirming it, and "probably the
        # same config" is not what a confirmation asserts. It stays PROMISING and
        # says why, which is actionable -- re-run it once and it confirms.
        confirmable = bool(cfgh)
        status = "CONFIRMED" if (independent >= 2 and confirmable) else "PROMISING"
        name = f"{label}@{cfgh}" if cfgh else label
        # The baseline and contract are part of the KEY, so they must be part of
        # the name -- otherwise two groups that differ only in contract collide
        # here and one silently overwrites the other.
        name = f"{name} [{baseline[:8]}/{contract[:8]}]"
        configs[name] = {
            "config": label,
            "config_digest": cfgh,
            "baseline": baseline,
            "contract": contract,
            "cleared_series": [list(k) for k in keys],
            "independent_cohorts": independent,
            "status": status,
            "blocked_by": "" if status == "CONFIRMED" else
                          ("no config digest: re-run to establish identity"
                           if not confirmable else
                           "needs a second non-overlapping cohort"),
        }

    scored = len(rows)
    registered = sum(1 for r in rows if r["prereg"]["registered"])
    return {
        "series": [_series_out(k, v) for k, v in
                   sorted(series.items(), key=lambda kv: kv[1]["as_of"])],
        "configs": configs,
        "health": {
            "scored_runs": scored,
            "pre_registered": registered,
            "pre_registered_pct": round(100.0 * registered / scored, 1)
            if scored else 0.0,
            "claims_kept": sum(1 for r in rows if r.get("claim") == "kept"),
            "claims_broken": sum(1 for r in rows if r.get("claim") == "broken"),
            # SURFACED, not swallowed. A loop whose claims are all unjudgeable is
            # producing pre-registrations that cannot be wrong, which is the same
            # failure as producing none -- and it would otherwise look healthy,
            # because `pre_registered` would read 100%.
            "reference_runs": sum(1 for r in rows
                                  if r.get("claim") == "reference"),
            # A NOTE REJECTED FOR A MALFORMED STRUCTURED FIELD, which is not the
            # same signal as a legacy hand-typed note: it means something tried
            # to write a pre-registration and produced one that could not be
            # refuted. That must be visible, not filed under "history".
            "malformed_preregs": sum(
                1 for r in rows if r["prereg"].get("tol_error")),
            "claims_unjudgeable": sum(
                1 for r in rows
                if str(r.get("claim", "")).startswith("unjudgeable")),
            "series_count": len(series),
            # THE COUNTER THE LOOP STEERS ON. Zero unrecorded means the first
            # action in the leader's prompt no longer matches, which is how a
            # tick becomes a NOOP instead of re-narrating yesterday's row.
            "unrecorded_runs": sum(1 for r in rows if not r.get("recorded")),
        },
    }


def _independent_count(windows):
    """The largest set of mutually non-overlapping holdout windows.

    Greedy by end date, which is exact for interval scheduling -- and the reason
    to be exact rather than to count distinct hashes is that three extracts one
    day apart are three hashes and one cohort.
    """
    known = sorted((w for w in windows if w is not None), key=lambda w: w[1])
    chosen = []
    for window in known:
        if all(not windows_overlap(window, picked) for picked in chosen):
            chosen.append(window)
    # An unknown window cannot be shown to be independent, so it contributes
    # nothing. It does not subtract either: the count is a floor.
    return len(chosen)


def _series_out(key, entry):
    return {
        "extract": key[0], "baseline": key[1], "contract": key[2],
        "as_of": entry["as_of"],
        "holdout": [d.date().isoformat() for d in entry["holdout"]]
        if entry["holdout"] else None,
        "runs": len(entry["rows"]),
        "frontier": entry["frontier"],
        "rows": [{"evaluation": r.get("evaluation", ""),
                  "when": r.get("when", ""),
                  "config": r["config_label"],
                  "verdict": r.get("verdict", "-"),
                  "bar": r["prereg"]["bar"],
                  "direction": r["prereg"]["direction"],
                  "claim": r.get("claim", "unregistered"),
                  "recorded": bool(r.get("recorded")),
                  "hypothesis": r["prereg"]["hypothesis"],
                  "metrics": r.get("metrics") or {},
                  "passed": r.get("passed") or {}}
                 for r in entry["rows"]],
    }


def render(report):
    out = ["# Research frontier", ""]
    h = report["health"]
    out.append(f"{h['scored_runs']} scored run(s) across {h['series_count']}"
               f" series. {h['pre_registered']} pre-registered"
               f" ({h['pre_registered_pct']}%);"
               f" {h['claims_kept']} claim(s) kept, {h['claims_broken']} broken,"
               f" {h['claims_unjudgeable']} unjudgeable.")
    if h.get("malformed_preregs"):
        out.append("")
        out.append(f"WARNING {h['malformed_preregs']} note(s) carry a malformed"
                   " structured field and are NOT counted as pre-registered."
                   " A tolerance that is negative, non-finite or unreadable"
                   " makes a claim unrefutable, so the note is rejected rather"
                   " than repaired.")
    if h["claims_unjudgeable"] > h["claims_kept"] + h["claims_broken"]:
        out.append("")
        out.append("WARNING more claims are unjudgeable than judged. A"
                   " pre-registration that cannot come out false is not one;"
                   " the usual cause is a missing `vs=` reference.")
    if h["scored_runs"] and h["pre_registered"] < h["scored_runs"]:
        out.append("")
        out.append(f"NOTE {h['scored_runs'] - h['pre_registered']} run(s) carry"
                   " no pre-registration. Rows scored before the loop existed"
                   " are history, not violations; new ones are violations.")

    out.append("")
    out.append(f"Unrecorded scored runs: {h['unrecorded_runs']}."
               " A run stays unrecorded until a RECORDED journal entry cites its"
               " id; an escalated entry does not count.")
    if h["unrecorded_runs"]:
        out.append("")
        out.append("| run | config | claim | needs writing up |")
        out.append("|---|---|---|---|")
        for entry in report["series"]:
            for r in entry["rows"]:
                if r["recorded"]:
                    continue
                out.append(f"| {r['evaluation']} | {r['config']} |"
                           f" {r['claim']} | yes |")

    out.append("")
    out.append("## Configs that cleared every bar")
    if not report["configs"]:
        out.append("")
        out.append("None yet.")
    for label, info in sorted(report["configs"].items()):
        out.append("")
        out.append(f"- **{label}** — {info['status']}"
                   f" ({info['independent_cohorts']} non-overlapping cohort(s),"
                   f" {len(info['cleared_series'])} series)")
        if info["blocked_by"]:
            out.append(f"  - {info['blocked_by']}")

    for entry in report["series"]:
        out.append("")
        out.append(f"## series {entry['extract'][:8]} — as_of"
                   f" {entry['as_of'] or '?'} — {entry['runs']} run(s)")
        window = entry["holdout"]
        out.append("")
        out.append(f"holdout {window[0]}..{window[1]}" if window
                   else "holdout UNKNOWN — this series cannot confirm anything,"
                        " because non-overlap cannot be shown")
        out.append("")
        out.append("| bar | best | config | passed |")
        out.append("|---|---|---|---|")
        for name in sorted(entry["frontier"]):
            best = entry["frontier"][name]
            mark = {True: "yes", False: "NO"}.get(best["passed"], "?")
            note = " (band: first seen)" if RANK.get(name) == "band" else ""
            out.append(f"| {name} | {best['value']:.4g}{note} |"
                       f" {best['config']} | {mark} |")
        out.append("")
        out.append("| when | config | verdict | claimed | outcome | written up |")
        out.append("|---|---|---|---|---|---|")
        for row in entry["rows"]:
            claimed = f"{row['direction']} {row['bar']}" if row["bar"] else "—"
            out.append(f"| {row['when']} | {row['config']} |"
                       f" {row['verdict']} | {claimed} | {row['claim']} |"
                       f" {'yes' if row['recorded'] else 'NO'} |")
    return "\n".join(out)


def _journal_dir(argv):
    for i, a in enumerate(argv):
        if a == "--journal" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--journal="):
            return a.split("=", 1)[1]
    return ""


def main(argv):
    raw = sys.stdin.read()
    try:
        rows = json.loads(raw)
    except ValueError:
        print("frontier.py reads `results.sh --json` on stdin", file=sys.stderr)
        return 2
    if not isinstance(rows, list):
        print("expected a list of scored rows", file=sys.stderr)
        return 2

    # A frontier without the extract windows cannot compute the confirm gate, so
    # it says so instead of quietly reporting every distinct hash as a cohort.
    extracts, contracts = {}, {}
    try:
        ok, body = experiment.qf("extracts", "--json", timeout=60)
        extracts = body if ok else {}
        ok, body = experiment.qf("contracts", "--json", timeout=60)
        contracts = body if ok else {}
    except Exception as e:                                  # noqa: BLE001
        print(f"note: cannot read inputs from qf ({e});"
              " overlap will report as unknown", file=sys.stderr)

    journal = _journal_dir(argv)
    # NO --journal MEANS NOTHING IS RECORDED, which makes every row unrecorded
    # and the report noisy rather than wrong. The alternative default -- treating
    # runs as handled when the journal cannot be read -- would silently retire
    # results the loop never wrote up.
    journaled = journaled_run_ids(journal) if journal else set()
    report = build(rows, extracts, contracts, journaled=journaled)
    if "--json" in argv:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
