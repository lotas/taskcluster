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

# HOW TO ORDER TWO `measured` VALUES -- DERIVED FROM THE CONTRACT, never
# hardcoded here.
#
# This replaces a hardcoded map that had `mae: lower`, which was WRONG and
# inverted every mae comparison the frontier made. `verdict.py:48-60` is the
# authority: for `relative_improvement` and `absolute_improvement` the scoreboard
# stores an IMPROVEMENT DELTA, computed as `baseline - value` for a
# lower-is-better metric -- so the metric's own `direction` has ALREADY been
# applied and higher is always better. Only `absolute` and `band` kinds store the
# raw metric value.
#
# The old map read `direction: lower_is_better` off the contract and concluded
# "lower measured is better", which is true of MAE the quantity and false of the
# number the scoreboard actually holds. A second source of truth about metric
# semantics is what made that possible, so there is no map any more.
_RANK_BY_KIND = {"relative_improvement": "higher",
                 "absolute_improvement": "higher",
                 "band": "band"}


def metric_ranks(contract):
    """`{metric: 'higher'|'lower'|'band'}` for this contract's metrics.

    A metric absent from the result is UNRANKABLE, and callers must treat that
    as "no ordering" rather than guessing one -- guessing is the bug above.
    """
    out = {}
    for name, spec in ((contract.get("metrics") or {}).items()):
        bar = (spec or {}).get("bar") or {}
        kind = bar.get("kind")
        if kind in _RANK_BY_KIND:
            out[name] = _RANK_BY_KIND[kind]
        elif kind == "absolute":
            # The raw metric, so its own direction decides.
            out[name] = "lower" if (spec or {}).get(
                "direction") == "lower_is_better" else "higher"
    return out


def load_contract(contracts, contract_hash):
    """The contract body: `holdout_days`, metric directions, and metric ROLES.

    The roles matter as much as the directions -- a `role: report` metric is
    measured and shown but does not decide, so a report reading this contract
    wrong would either gate on an alert or hide a real gate.

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


def gate_metrics(contract):
    """The metric names that DECIDE under this contract. A `role: report`
    metric is shown beside the gates and must not make or break PROMISING:
    that is the whole reason the role exists."""
    return {name for name, spec in ((contract.get("metrics") or {}).items())
            if (spec or {}).get("role", "gate") == "gate"}


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

# RETIREMENT NEEDS A TARGET, NOT A MENTION. `_RUN_ID` is loose on purpose --
# scraping IDS out of prose is right for "has this run been written up at all",
# but wrong for "which run does THIS rejection count against": a rejected entry
# names its target, its comparator (`vs=`), the probe the evaluation came from
# AND the evaluation of it in the same paragraph, so scraping would retire up to
# four runs off one rejection, and a harmless rewrite that started citing the
# evaluation instead of the probe would look like a change of target. So exactly
# one declared field decides, and everything else in the file is evidence, never
# a target.
#
# BYTE-EXACT ON PURPOSE. `**Target run**:` (colon outside the bold) or
# `**target run:**` would not match, and that is deliberate rather than an
# oversight: the label is emitted by `tick-prompt.md`'s fixed template, not
# typed freehand, so the one spelling the template produces is the only one
# that needs to match.
_TARGET = re.compile(r"^\*\*Target run:\*\*[ \t]*(\S+)[ \t]*$", re.M)

# EVALUATION IDS ONLY. `build()`'s `index` maps an id to a LIST of rows because
# one probe can be scored under two contracts -- a probe id does not identify a
# single row, so a target expressed as a probe id would retire two series' worth
# of work off one rejection. A malformed or probe-shaped target is refused by
# `target_of`, not coerced.
_EVAL_ID = re.compile(r"^evaluate-[0-9A-Za-z]+-[0-9a-f]+-\d+$")

# ONLY A REAL REJECTION RETIRES. The tick also escalates when the verifier never
# returned a usable verdict at all -- an infrastructure outage, not a judgement --
# and two `codex` timeouts must never retire a write-up nobody actually read. A
# file with no parseable line here is `unknown`: NOT defaulted to `rejection`,
# because the historical files this reads predate the line and include exactly
# the outages this exists to exclude, and defaulting them in would cause the
# harm retirement exists to prevent.
_KIND = re.compile(r"^Escalation kind:[ \t]*(rejection|verifier-failure)[ \t]*$",
                   re.M)

# A WAIVER STARTS A NEW EPISODE; IT DOES NOT SUBTRACT. Three rejections against a
# threshold of two, minus one waiver, must still retire -- subtraction would
# revive the one case retirement exists for. So a waiver only moves the FLOOR: an
# escalation committed at or before the latest waiver for its target does not
# count, and one committed after does, in full.
_WAIVER = re.compile(r"^\*\*Waiver:\*\*[ \t]*(\S+)[ \t]*$", re.M)

# `YYYYMMDDTHHMMSSZ` SORTS LEXICOGRAPHICALLY LIKE A TIMESTAMP -- that is what the
# naming convention buys, and it is why a waiver's ordering against an escalation
# can be a string comparison instead of a datetime parse.
#
# BUT THE SHAPE IS NOT A DATE, and for a WAIVER that difference was Critical:
# `20270908T000000Z.md` (a typo'd year, or a box whose clock is skewed) matches
# this exactly, sets `floor[rid]` above every stamp the loop will produce for a
# year, and waives every rejection episode there is or will be -- so the row
# stays `unrecorded` and therefore both selectable AND suppressible, the drift
# streak never advances past the stored `last-reject-target`, every usable
# verdict resets the verifier-failure counter, and retirement never reaches its
# threshold. The 2026-09-04 hourly livelock with BOTH brakes off, from one
# mistyped filename, with nothing on any surface saying so. `_as_time` and
# `_WAIVER_LEAD` below are what a waiver stamp must now survive.
_STAMP = re.compile(r"^(\d{8}T\d{6}Z)")

# HOW FAR AHEAD OF THE JOURNAL'S OWN NEWEST COMMITTED FILE A WAIVER STAMP MAY
# SIT AND STILL BE BELIEVED. Not "ahead of now": `escalation_targets` decides
# what work the loop offers, so it stays a pure function of committed content --
# a wall-clock check would make one journal answer differently on two boxes, and
# on the box whose clock is wrong (the failure being defended against) it would
# answer wrongly. The journal's own stamps are the only clock available that is
# part of the input.
#
# ZERO SLACK IS NOT AN OPTION: a real waiver is always the newest file in the
# journal at the moment it is written, so "ahead at all" would void every
# legitimate revival. THE SLACK IS SMALL ON PURPOSE, because it is also the
# window a believed stamp can eat out of the FUTURE -- every hour inside it is an
# hour of rejections waived before they happen. Seven days covers the 2026-09-04
# shape (silent 2.5 days, human notices, writes a waiver) with margin, and caps
# the damage of a plausible-looking typo at a week instead of a year. Being
# wrong the tight way costs a named report line and a re-stamped filename;
# being wrong the loose way costs that many days of hourly livelock.
_WAIVER_LEAD = datetime.timedelta(days=7)

# A HUMAN-AUTHORED MIGRATION RECORD, not a second escalation format. Historical
# escalations predate `Escalation kind:` entirely, and some of those are the very
# outages this file exists to exclude -- so a missing line must read as
# `unknown`, never as `rejection`. `escalation-kinds.md` lets a human classify
# those old files after the fact without rewriting committed journal entries.
_KIND_LINE = re.compile(r"^(\d{8}T\d{6}Z)[ \t]+(rejection|verifier-failure)$",
                        re.M)


def _stamp_of(name):
    """The `YYYYMMDDTHHMMSSZ` prefix of a committed basename, or `""`.

    `""` sorts before every real stamp, so a name this cannot parse never wins a
    floor comparison and never overrides a classification -- it is simply
    unordered, the same "cannot answer" convention as the rest of this file.
    """
    m = _STAMP.match(name)
    return m.group(1) if m else ""


def _as_time(stamp):
    """A `YYYYMMDDTHHMMSSZ` stamp as a datetime, or `None` if it names no real
    instant.

    `_stamp_of` answers "does this filename carry a stamp"; this answers "does
    that stamp exist as a moment in time". `20261345T996000Z` passes the first
    and fails the second, and it sorts above every real 2026 stamp -- which is
    the same hole as a typo'd year, reached without the filename ever looking
    like a plausible date. `None` is the file's usual "cannot answer", and every
    caller must read it as "do not believe this stamp".
    """
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError):
        return None


def target_of(text):
    """The declared target of a journal entry: a canonical evaluation id, or
    `""` when the entry does not declare one, declares one that is not an
    evaluation id, or declares it more than once WITH DISAGREEMENT.

    Exactly one line is meant to decide this -- see `_TARGET` above for why
    scraping the body would be wrong four different ways. But "exactly one" is
    a claim about a well-formed file, not something the regex enforces by
    itself: an escalation that quotes a PREVIOUS entry's `**Target run:**` line
    inside a fenced evidence block -- entirely plausible, since the copilot's
    job is to show its work -- has two matches, and `.search` taking the first
    one would resolve to whichever happens to come first in the file. That is
    exactly the failure this field exists to prevent, just moved from "which
    field" to "which occurrence".

    So EVERY match is collected, not the first: identical repeats (a file
    quoting its own target line) are harmless and resolve normally, but
    DISAGREEING matches return `""` -- ambiguous, retires nothing -- rather
    than being resolved by position. Position is exactly the thing an
    agent-written markdown file does not guarantee, so refusing is the only
    safe reading; the drift shows up as the run staying unretired, never as a
    wrong run being retired.

    `_EVAL_ID` is checked on the (single, agreed) candidate, not just matched
    loosely by `_TARGET`, so a `**Target run:** probe-...` line (or a typo, or
    a comparator pasted into the wrong field) reads as NO target rather than as
    a wrong one.
    """
    matches = set(_TARGET.findall(text or ""))
    if len(matches) != 1:
        return ""
    candidate = next(iter(matches))
    return candidate if _EVAL_ID.match(candidate) else ""


def _committed_blobs(journal_dir, subdir=None):
    """`{basename: text}` for every `.md` blob committed at HEAD, one namespace.

    `subdir=None` means the flat journal root; `subdir="escalations"` means that
    directory and no deeper. `None` is returned when the question cannot be
    answered -- no repository, no git, no HEAD, a malformed listing or a failed
    call -- and EVERY caller must read that as "nothing counts" rather than
    "everything counts": the alternative silently retires results.

    BLOB OIDS, NEVER PATHS. `ls-tree` run with `-C <journal_dir>` prints paths
    relative to that directory, while `cat-file`'s path syntax resolves from the
    repository root, so `HEAD:<path>` from here is `fatal: path ... does not
    exist in 'HEAD'`. An oid needs no prefix and cannot be misresolved.

    LINE ENDINGS ARE NORMALIZED HERE, ONCE, because this is the one place
    journal TEXT enters the module -- `_parse_batch` hands back exact blob
    bytes decoded, on purpose, since its job is byte-accurate framing, not
    content semantics. Every regex below is anchored with `$` under `re.M`,
    which matches before `\n` but NOT before a `\r` that a CRLF-saved file
    would leave in front of it, so a `\r\n`-authored file would silently
    fail every one of them: `target_of` would return `""`, the escalation
    would `continue` before its kind was even classified, and it would count
    toward NEITHER `targets` NOR `unclassified` -- the one invisible failure
    mode in this file, where everywhere else "cannot answer" is plumbed into
    a counter an operator can see. Normalizing at this single choke point
    means a future regex added here inherits the fix for free, rather than
    needing its own `\r?$` that is easy to forget.
    """
    import subprocess
    try:
        listed = subprocess.run(
            ["git", "-C", journal_dir, "ls-tree", "-z", "-r", "HEAD"],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None

    parsed = _parse_ls_tree(listed.stdout, subdir)
    if parsed is None:
        return None
    oids, names = parsed
    if not oids:
        return {}
    try:
        done = subprocess.run(
            ["git", "-C", journal_dir, "cat-file", "--batch"],
            input=("\n".join(oids) + "\n").encode(),
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    blobs = _parse_batch(done.stdout, names)
    if blobs is None:
        return None
    return {name: text.replace("\r\n", "\n").replace("\r", "\n")
            for name, text in blobs.items()}


def _parse_ls_tree(stdout, subdir):
    """`(oids, names)` for the `.md` blobs in one flat namespace, or `None`.

    `stdout` is `git ls-tree -z -r HEAD` run from inside `journal_dir`, so every
    path is relative to it: a bare name is the journal root, `escalations/x.md`
    is one level down. `None` on any framing surprise -- a NUL-delimited record
    with no tab, or a `<mode> <type> <oid>` header that is not exactly three
    fields -- for the same reason `_parse_batch` refuses rather than guesses: a
    malformed listing must read as "answer unknown", never as "empty".

    A TAB OR NEWLINE INSIDE A FILENAME DOES NOT DESYNCHRONISE THIS, and that
    rests on two properties, not one: `-z` NUL-delimits each ENTRY, so a stray
    tab or newline byte inside a path can never be mistaken for the separator
    between entries, and `partition(b"\t")` splits on the FIRST tab only, so a
    literal tab inside the filename itself stays part of `path` rather than
    truncating it.
    """
    oids, names = [], []
    for raw in stdout.split(b"\0"):
        if not raw:
            continue
        meta, tab, path = raw.partition(b"\t")
        if not tab:
            return None                     # not the documented format: refuse
        fields = meta.split(b" ")
        if len(fields) != 3:
            return None
        if fields[1] != b"blob":
            continue
        name = path.decode("utf-8", "replace")
        if not name.endswith(".md"):
            continue
        # DEPTH IS ENFORCED ON THE RETURNED PATHS, not by a pathspec: a pathspec
        # of `.` still returns nested entries (verified), so the filter has to
        # be here or `escalations/` would leak into the journal root's reader.
        parts = name.split("/")
        if subdir is None:
            if len(parts) != 1:
                continue
        elif len(parts) != 2 or parts[0] != subdir:
            continue
        oids.append(fields[2].decode("ascii", "replace"))
        names.append(parts[-1])
    return oids, names


def _parse_batch(buf, names):
    """Pair `cat-file --batch` output with the names it was asked about.

    `--batch` answers IN INPUT ORDER, so pairing by index is sound. Each answer
    is `<oid> SP <type> SP <size> LF <bytes> LF`.

    `None` on ANY framing surprise, and that is the whole point: slicing past the
    end of `bytes` does not raise in Python, so an unchecked parser answers a
    truncated response with partial content spliced together from two records --
    a silently wrong answer about whether a result was already written up.
    """
    out, pos = {}, 0
    for name in names:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            return None
        fields = buf[pos:nl].split(b" ")
        if len(fields) != 3:
            return None
        _oid, kind, size_s = fields
        # `blob` IS CHECKED, because `--batch`'s framing is uniform across
        # object types but only a blob's size means "content bytes"; a
        # desynchronised pairing that happens to land on a `commit` or `tree`
        # header would otherwise be read as if it were one.
        if kind != b"blob":
            return None
        try:
            size = int(size_s)
        except ValueError:
            return None
        if size < 0:
            return None
        start = nl + 1
        end = start + size
        # THE FRAMING ASSERTION THAT CATCHES A WRONG SIZE EVEN WHEN ENOUGH BYTES
        # HAPPEN TO BE PRESENT. `--batch` always appends exactly one trailing
        # newline after the content, so if the byte at `end` is not it, `size`
        # was wrong -- and slicing `buf[start:end]` without this check does not
        # raise, it just returns fewer bytes than promised, silently splicing in
        # a fragment of the next record.
        if buf[end:end + 1] != b"\n":
            return None
        out[name] = buf[start:end].decode("utf-8", "replace")
        pos = end + 1
    return out


# THE JOURNAL ROOT'S CONTRACT IS "EVERY COMMITTED `.md` HERE IS AN ENTRY" --
# `journaled_run_ids` reads the whole root and scrapes every blob with the
# deliberately loose `_RUN_ID` regex, so anything committed there that is NOT
# an entry has to be enumerated as an exception, not discovered by discipline.
# Two are known today: `PENDING.md` (never committed, guarded cheaply anyway)
# and `escalation-kinds.md` (a human-authored control file -- see
# `journaled_run_ids` for the hazard of leaving it unguarded). Collected into
# one set rather than two separate `if name ==` sites so a third control file
# has exactly one place to be added -- and `RootEntryNaming`'s test below
# turns forgetting to add it into a loud, failing assertion instead of a
# silent repeat of this exact bug, which is how `escalation-kinds.md` arrived
# in the root in the first place: the same hazard, reasoned through for
# `waivers/` and then not re-applied here.
_NOT_ENTRIES = {"PENDING.md", "escalation-kinds.md"}

# AN ENTRY'S NAME, for the same tripwire: `<stamp>.md`, nothing else. Used only
# to recognise a root file as an entry for the invariant check above, never to
# gate `journaled_run_ids` itself -- that function reads EVERY entry's text
# regardless of naming, and tightening it to require this shape is a
# different, larger change than the one this pattern exists for.
_ENTRY_NAME = re.compile(r"^\d{8}T\d{6}Z\.md$")


def _unrecognized_root_names(names):
    """Journal-root basenames that are neither a stamped entry (`_ENTRY_NAME`)
    nor a known control file (`_NOT_ENTRIES`).

    THE FIXTURE-LEVEL SUBSTITUTE FOR A REPO-LEVEL ASSERTION THAT CANNOT BE
    WRITTEN: the real journal lives on the host this loop runs on, not in this
    repository, so there is no shipped `journal/` tree here to check against
    directly. This function is the same check with the root's contents
    supplied by a test instead of by disk, and it exists so that a THIRD
    control file -- added the way `escalation-kinds.md` was, by reasoning
    through the hazard for one file and not re-running that reasoning for the
    next one -- shows up here as unrecognized rather than silently being
    scraped by `journaled_run_ids` like an entry. It does not gate anything by
    itself; it is a name for the invariant a test can pin.
    """
    return sorted(n for n in names
                  if n not in _NOT_ENTRIES and not _ENTRY_NAME.match(n))


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

    RETIREMENT IS NOT A RELAXATION OF THIS. `escalation_targets` (added in a
    later task) counts escalations separately and marks a run `retired` in its
    own state; an escalated entry still never makes a run look recorded here.
    """
    blobs = _committed_blobs(journal_dir)
    if blobs is None:
        # CANNOT TELL COMMITTED FROM UNCOMMITTED: count NOTHING. Every run then
        # reads as unrecorded, which is noisy and safe; the opposite default --
        # treating an unreadable journal as fully written up -- would silently
        # retire results nobody ever committed.
        return set()
    seen = set()
    for name, text in blobs.items():
        # `PENDING.md` CANNOT BE COMMITTED (a cheap guard anyway) and
        # `escalation-kinds.md` IS A CONTROL FILE, NOT AN ENTRY, but it lives
        # in the journal root rather than its own subdirectory -- unlike
        # `waivers/`, which got a subdirectory of its own for exactly this
        # reason. `_RUN_ID` is loose on purpose (see its own comment) and
        # reads the WHOLE file, not just the `<stamp> <kind>` lines the
        # classifier cares about, so a run id anywhere in it -- a header
        # comment, a note above the table, prose explaining a decision --
        # gets scraped out identically to one in a real entry. For example:
        #
        #   # classified 2026-09-04, per the postmortem for
        #   # evaluate-20260904T090941Z-0a217f40da8f-6446
        #   20260904T101126Z rejection
        #
        # Without this guard, that comment alone would make the named run
        # read as RECORDED -- not retired, RECORDED -- so the leader never
        # picks it up again and the finding is lost silently and permanently.
        # That is a worse outcome than the escalation exclusion two paragraphs
        # up exists to prevent, and reachable by the file's ordinary use: a
        # human classifying escalations by hand will very plausibly leave a
        # note like this for their own benefit. (A run id typed directly onto
        # the classified line itself, as `_KIND_LINE` requires it, would not
        # even classify -- that line is fully anchored, so trailing prose
        # makes it fail to match at all, silently, rather than leak. Both are
        # real failure modes of this file; they are just different ones.)
        #
        # NAME-SKIP CHOSEN OVER A SUBDIRECTORY: moving the file to its own
        # subdirectory (`_committed_blobs(journal_dir, "kinds")`, mirroring
        # `waivers/`) would also close this, and the migration itself is a
        # bigger reason to prefer NOT doing it than the migration's cost: for
        # however long the root copy is deleted and a reader for the new
        # subdirectory is not yet deployed, EVERY override silently
        # disappears, every already-classified historical escalation reverts
        # to `unknown`, and retirement quietly stops happening -- fail-safe in
        # direction (nothing gets wrongly retired) but entirely invisible,
        # which is the same character of bug this guard exists to close, just
        # relocated to a deploy window instead of a comment. One line, for the
        # one well-known file that exists today, has no such window.
        if name in _NOT_ENTRIES:
            continue
        seen.update(_RUN_ID.findall(text))
    return seen


def escalation_targets(journal_dir):
    """`({run id: {rejections, latest}}, {"unclassified": [...],
    "unparseable_waivers": [...], "misdated_waivers": [...]})`,
    the three problem lists holding BASENAMES (`20260904T101126Z.md`, not
    `escalations/20260904T101126Z.md`) rather than counts -- see the note
    below the counting rules for why a count alone is not enough. Bare, not
    prefixed with their directory: the caller (`render()`) already says which
    directory once, for the whole list, so a per-name prefix would repeat it.

    This is the counting half of retirement (task 3 turns it into a
    `recorded|unrecorded|retired` state per row). Every rule below exists
    because of a specific way a naive count would retire the wrong thing:

    ONE FILE IS ONE EPISODE. A rejection is counted once per escalation file
    regardless of how many times its target is repeated inside it (a table, a
    quoted diff, a restated hypothesis) -- the copilot rejected the write-up
    once, not once per mention.

    ONLY `rejection` COUNTS. `verifier-failure` (the verifier never returned a
    usable verdict -- an infrastructure outage) is skipped entirely: it is not
    evidence the claim was wrong, so it must not touch the count OR
    `problems["unclassified"]`. `unknown` (no parseable kind line, which is
    every escalation from before the line existed) counts toward the GLOBAL
    `problems["unclassified"]` so it is visible to an operator, but never
    toward any run's `rejections` -- historical escalations include exactly
    the outages this function exists not to retire on, and defaulting an
    unparseable line to `rejection` would cause that harm.

    UNCLASSIFIED IS COUNTED ONLY GLOBALLY, DELIBERATELY, NOT PER RUN. There is
    no per-id `unknown` field: an `unknown` escalation names a run whose
    write-up may never have actually been judged, so attributing it to that run
    would claim something this file cannot establish -- and a run whose
    escalations are ALL `unknown` would get no record at all, so a per-id count
    would be silently wrong for exactly the runs it matters most for (it would
    read as zero rather than as "unknown"). The global total is honest about
    what it knows: that many escalations could not be classified, full stop.

    A WAIVER STARTS A NEW EPISODE, IT DOES NOT SUBTRACT. Only escalations
    committed strictly after the latest waiver for a target count; that is
    a floor on the timeline, not arithmetic against the running total, so a
    waiver can never revive a run that racked up more rejections than the
    threshold before it was granted.

    `latest` IS CARRIED because the report needs to name the newest qualifying
    file, and a bare count cannot reconstruct that -- particularly across a
    waiver, where the newest ESCALATION is not the newest QUALIFYING one.

    A WAIVER CANNOT OUTRUN THE JOURNAL. The stamp is a filename, typed by a
    human, and `_stamp_of` only ever checked its SHAPE -- so a typo'd year or a
    skewed clock (`waivers/20270908T000000Z.md`) put the floor above every stamp
    the loop would produce for a year and waived every episode there was or
    would be, leaving the row `unrecorded` (still selectable, still
    suppressible), the drift streak stuck on a stored `last-reject-target`, the
    verifier counter reset by every usable verdict, and retirement unreachable:
    the 2026-09-04 livelock with both brakes off. So a waiver stamp must name a
    real instant AND sit within `_WAIVER_LEAD` of the journal's own newest
    committed entry or escalation; one that does not is `misdated_waivers` and
    revives NOTHING, the same way an undateable filename always has. NO CLOCK IS
    READ: the comparison is between two committed stamps, so this function stays
    a pure function of committed content -- it decides what work the loop
    offers, and that must not depend on which box asks.

    THE SECOND RETURN VALUE IS A DICT OF LISTS, NOT A DICT OF COUNTS. A count
    told an operator "1 file(s), go find it" and left the finding to `ls` or a
    hand-diff against `escalation-kinds.md` -- the same silence one level up
    from what this function exists to end. `unclassified` keeps its exact
    meaning above but now NAMES each escalation, and `unparseable_waivers`
    likewise names each waiver file whose basename does not start with a stamp
    `_stamp_of` can read -- the ONE file type whose entire purpose is
    operator-initiated revival was, before this counter existed, the one kind
    of malformed input with no operator-visible trace at all: a misnamed
    waiver revives nothing, however many valid `**Waiver:**` lines it holds,
    and the floor loop's `continue` used to make that failure as silent as
    success. Callers that only want the count take `len(...)` -- `build()`
    does exactly that for `health`, whose shape stays int-valued.
    """
    esc = _committed_blobs(journal_dir, "escalations")
    waivers = _committed_blobs(journal_dir, "waivers")
    root = _committed_blobs(journal_dir)
    if esc is None:
        # CANNOT TELL COMMITTED FROM UNCOMMITTED: count NOTHING, the same
        # convention as `journaled_run_ids` -- an unreadable journal must never
        # read as "no rejections happened".
        return {}, {"unclassified": [], "unparseable_waivers": [],
                    "misdated_waivers": []}

    # THE OVERRIDE WINS WHENEVER IT IS PRESENT, even over a file that also
    # carries its own `Escalation kind:` line -- the code checks
    # `overrides.get(stamp)` first and only falls back to the file's line when
    # that lookup misses. This is deliberate, not merely "fills the gap for
    # files that predate the line": `escalation-kinds.md` exists so a human can
    # CORRECT a classification, not only supply one that is missing, and a
    # record that could never override an existing line would not be able to
    # do that job.

    overrides = {}
    for stamp, kind in _KIND_LINE.findall((root or {}).get(
            "escalation-kinds.md", "")):
        overrides[stamp] = kind

    # THE WAIVER FLOOR, keyed by target and kept at the LATEST stamp seen --
    # because a second waiver for the same run only ever extends the window it
    # protects, never shrinks it.
    #
    # A WAIVER CAN ONLY WAIVE INSIDE THE TIMELINE THE JOURNAL ITSELF SHOWS. The
    # journal's newest committed stamp -- newest entry or newest escalation, NOT
    # newest waiver, because a mistyped waiver must never be the thing that
    # certifies the next one -- is the only clock in this function's INPUT, and
    # a waiver more than `_WAIVER_LEAD` past it is not a grant time, it is a
    # typo or a skewed clock. See `_STAMP` for what believing one cost.
    #
    # WAIVERS ARE EXCLUDED FROM THE MARK, ESCALATIONS AND ENTRIES ARE NOT,
    # because those two are written by the loop, one per tick, and are therefore
    # the record of how far the loop has actually got.
    latest_committed = None
    for name in list(root or {}) + list(esc):
        when = _as_time(_stamp_of(name))
        if when and (latest_committed is None or when > latest_committed):
            latest_committed = when

    problems = {"unclassified": [], "unparseable_waivers": [],
                "misdated_waivers": []}
    floor = {}
    for name, text in (waivers or {}).items():
        stamp = _stamp_of(name)
        if not stamp:
            # SURFACED, NOT SWALLOWED -- see the docstring. A waiver that
            # cannot be dated cannot set a floor, and silently doing nothing
            # is exactly the failure mode this counter exists to end. THE
            # BASENAME IS KEPT, not just the count -- `render()` says "in
            # waivers/" once and lists names underneath it.
            problems["unparseable_waivers"].append(name)
            continue
        when = _as_time(stamp)
        if when is None or (latest_committed is not None and
                            when - latest_committed > _WAIVER_LEAD):
            # NOT BELIEVED, AND NOT CLAMPED. Clamping the floor down to the
            # newest escalation for the id looks safer and is not: that ceiling
            # is recomputed from whatever is committed AT READ TIME, so every
            # NEW escalation raises it to its own stamp and `<=` then waives
            # that one too -- the count sits at zero permanently, where the
            # un-clamped defect at least expired when the typo'd year arrived.
            # Nor is it fatal: a misnamed waiver must not be able to refuse the
            # whole frontier, because the report is what a human reads when the
            # loop is stuck. So it does exactly what an undateable waiver has
            # always done -- revives NOTHING -- and says which file, by name,
            # for the one person who can re-stamp it.
            problems["misdated_waivers"].append(name)
            continue
        for rid in _WAIVER.findall(text):
            if _EVAL_ID.match(rid) and stamp > floor.get(rid, ""):
                floor[rid] = stamp

    targets = {}
    for name in sorted(esc):
        rid = target_of(esc[name])
        if not rid:
            continue
        stamp = _stamp_of(name)
        kind = overrides.get(stamp)
        if not kind:
            m = _KIND.search(esc[name])
            kind = m.group(1) if m else "unknown"
        if kind == "verifier-failure":
            continue                 # an outage, not a verdict: not evidence
        if kind == "unknown":
            # GLOBAL ONLY. See the docstring: no per-id field exists to put
            # this in, and that is on purpose. NAMED (basename, see the
            # docstring) rather than merely counted.
            problems["unclassified"].append(name)
            continue
        # kind == "rejection"
        rec = targets.setdefault(rid, {"rejections": 0, "latest": ""})
        # `<=`, NOT `<`, ON PURPOSE: the design says an escalation counts only
        # when committed AFTER the latest waiver, so a stamp exactly equal to
        # the waiver's is still waived. `test_a_waiver_with_the_same_stamp_...`
        # pins this so it stays a decision rather than an accident of whichever
        # operator got typed here.
        if stamp and stamp <= floor.get(rid, ""):
            continue                 # waived: this episode predates the grant
        rec["rejections"] += 1
        rec["latest"] = f"escalations/{name}"
    return targets, problems


class FrontierError(Exception):
    """A frontier input that violates an invariant this file enforces.

    Mirrors `prereg.PreregError`'s reasoning: a value that violates an
    invariant should fail loudly rather than be silently coerced. Not
    `SystemExit` -- this module is imported, not just run as a script, and
    `SystemExit` is a `BaseException` that `except Exception` (and every test
    that does not know to look for it) would let straight through.
    """


def retire_after():
    """How many rejection episodes retire a run. Never zero.

    A `0` would retire every unrecorded row on sight, so an out-of-range or
    unparseable value is FATAL rather than a silent fallback: this number is one
    of the two things that decide whether the loop keeps offering work, and a
    typo in a systemd unit must not quietly switch it off.
    """
    raw = os.environ.get("QF_FRONTIER_RETIRE_AFTER", "2")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise FrontierError(
            f"QF_FRONTIER_RETIRE_AFTER must be an integer >= 1, got {raw!r}")
    if n < 1:
        raise FrontierError(
            f"QF_FRONTIER_RETIRE_AFTER must be an integer >= 1, got {n!r}")
    return n


def series_key(row):
    return (row.get("extract", ""), row.get("baseline", ""),
            row.get("contract", ""))


def better(value, incumbent, how):
    """Is `value` a better measurement than `incumbent`, given its ranking?

    `how` comes from `metric_ranks`. `None` means the contract could not be read,
    and then there is NO ordering: the first measurement stands and the report
    says the series is unordered. Inventing a direction here is exactly the
    defect this signature exists to prevent.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if incumbent is None:
        return True
    if how == "lower":
        return value < incumbent
    if how == "higher":
        return value > incumbent
    return False        # `band` or unknown: the first measurement stands


def _band_distance(value, band):
    """How far outside `(low, high)` a value sits. Zero when inside."""
    low, high = band
    if low is not None and value < low:
        return low - value
    if high is not None and value > high:
        return value - high
    return 0.0


def judge_claim(row, index, rank=None, band=None):
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

    how = rank

    if reg["direction"] == "hold":
        # NUMERIC, not pass/fail-status. Status equality was wrong in both
        # directions: a tail miss going 0.304 -> 0.900 would have read as "kept"
        # because both fail the 0.30 bar, and a fail -> pass improvement would
        # have read as "broken". `hold` means "did not get worse", so it is a
        # comparison of values with the tolerance the run pre-registered.
        #
        # The tolerance is in the immutable note, so the slack was claimed before
        # the number existed. Default 0 means strictly not worse.
        if how is None:
            return "unjudgeable: no contract, so the metric has no ordering"
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

    if how is None:
        # NO CONTRACT, NO ORDERING. Without knowing whether `measured` is a raw
        # metric or an improvement delta, "improve" cannot be checked -- and
        # guessing inverted every mae claim once already.
        return "unjudgeable: no contract, so the metric has no ordering"
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


def build(rows, extracts, contracts, journaled=(), escalations=None,
          problems=None, retire_after_n=None):
    """Group into series, then roll configs up across them.

    `retire_after_n` is the resolved threshold, not the environment: `build()`
    is otherwise pure, taking every external input as an argument (`journaled`,
    `escalations`, `problems` are the same shape), and reading `os.environ`
    directly here would be the one exception. `None` (the default, and what
    `main()` never has to pass explicitly) resolves it via `retire_after()`, so
    a caller that doesn't care about the threshold still gets the systemd-unit
    behaviour, and a test that does care can pass a value directly instead of
    mutating process environment.

    Raises `FrontierError` -- not `SystemExit` -- if the threshold is
    unparseable or out of range; see `retire_after()` and `FrontierError`.
    """
    # COMPUTED FIRST, BEFORE ANY WORK: an out-of-range or unparseable threshold
    # must fail before a single row is processed, not partway through.
    limit = retire_after() if retire_after_n is None else retire_after_n
    by_hash = {e.get("request_hash"): e for e in extracts.get("extracts") or []}

    series = {}
    for row in rows:
        key = series_key(row)
        entry = series.setdefault(key, {
            "extract": key[0], "baseline": key[1], "contract": key[2],
            "rows": [], "frontier": {}, "as_of": "", "holdout": None,
            "ordered": False, "ranks": {}})
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
        # THE STATE THE WHOLE LOOP STEERS ON. Two values could not express
        # "rejected twice, not written up, and not to be offered again", which
        # is the livelock of 2026-09-04: `tick-prompt.md` action 1 matches
        # `written up: NO` forever, so one unrecordable run consumed every tick.
        esc = (escalations or {}).get(row.get("evaluation") or "") or {}
        row["rejections"] = esc.get("rejections", 0)
        row["escalation_latest"] = esc.get("latest", "")
        if row["recorded"]:
            row["handling"] = "recorded"
        elif row["rejections"] >= limit:
            row["handling"] = "retired"
        else:
            row["handling"] = "unrecorded"
        for ident in (row.get("evaluation"), row.get("probe")):
            if ident:
                index.setdefault(ident, []).append(row)

    for key, entry in series.items():
        contract = load_contract(contracts, entry["contract"])
        extract = by_hash.get(entry["extract"], {})
        entry["as_of"] = extract.get("as_of_date") or ""
        entry["holdout"] = holdout_window(extract, contract)
        ranks = metric_ranks(contract)
        bands = _band_bounds(contract)
        entry["ordered"] = bool(ranks)
        entry["ranks"] = ranks
        # STASHED, so the `cleared` loop below and `render` both read the same
        # gate set this contract was loaded once for.
        entry["gates"] = gate_metrics(contract)
        for row in entry["rows"]:
            row["claim"] = judge_claim(row, index,
                                       rank=ranks.get(row["prereg"]["bar"]),
                                       band=bands.get(row["prereg"]["bar"]))
            for name, value in (row.get("metrics") or {}).items():
                slot = entry["frontier"].get(name)
                if better(value, (slot or {}).get("value"), ranks.get(name)):
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
        # THE GATES ONLY. A `role: report` metric is measured and shown but does
        # not decide, so counting its flag here would let a reported alert veto a
        # config that cleared every gate -- v2 demotes the 30m+ miss rate for
        # exactly that reason. An unreadable contract yields no gate names, in
        # which case every reported flag still counts, as it did before roles.
        gates = entry["gates"]
        for row in entry["rows"]:
            flags = {k: v for k, v in (row.get("passed") or {}).items()
                     if not gates or k in gates}
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
    # `escalation_targets` NOW RETURNS NAMES, NOT COUNTS (see its docstring):
    # "1 file(s), go find it" made an operator go `ls` or hand-diff every
    # escalation to find the one that mattered. `health` stays INT-VALUED --
    # nothing downstream of the JSON shape has to change -- so the names are
    # carried separately, in `problems`, for `render()` to spell out.
    unclassified = sorted((problems or {}).get("unclassified") or [])
    unparseable_waivers = sorted((problems or {}).get(
        "unparseable_waivers") or [])
    # A WAIVER THE READER REFUSED TO BELIEVE (a stamp that names no real instant,
    # or one implausibly ahead of the journal's own newest file) revives nothing
    # -- and a waiver is the one file type whose entire purpose is
    # operator-initiated revival, so it travels the same named route as an
    # unparseable one rather than being clamped in silence.
    misdated_waivers = sorted((problems or {}).get("misdated_waivers") or [])
    return {
        "series": [_series_out(k, v) for k, v in
                   sorted(series.items(), key=lambda kv: kv[1]["as_of"])],
        "configs": configs,
        "problems": {
            "unclassified": unclassified,
            "unparseable_waivers": unparseable_waivers,
            "misdated_waivers": misdated_waivers,
        },
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
            # RETIRED ROWS ARE EXCLUDED HERE ON PURPOSE: this is the counter
            # that used to make a twice-rejected, unwritable run look like
            # unfinished work forever (the 2026-09-04 livelock), and a retired
            # row is no longer offered, so it must stop inflating this number
            # the same way a recorded one does.
            "unrecorded_runs": sum(1 for r in rows
                                   if r.get("handling") == "unrecorded"),
            "retired_runs": sum(1 for r in rows
                                if r.get("handling") == "retired"),
            # INT-VALUED, DELIBERATELY: the health JSON shape does not change
            # just because the source data grew names. See `problems` above
            # for the list a report renderer actually names files from.
            "unclassified_escalations": len(unclassified),
            "unparseable_waivers": len(unparseable_waivers),
            "misdated_waivers": len(misdated_waivers),
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
        "ordered": entry["ordered"],
        "ranks": entry["ranks"],
        # A LIST, because the report is serialised as JSON and a set is not.
        "gates": sorted(entry.get("gates") or ()),
        "holdout": [d.date().isoformat() for d in entry["holdout"]]
        if entry["holdout"] else None,
        "runs": len(entry["rows"]),
        "frontier": entry["frontier"],
        # `probe` IS CARRIED, because the claims that action 1 produces are
        # mostly ABOUT the probe/evaluation relationship: which evaluations
        # re-score the same probe, which probes have no scoreboard, how many
        # distinct models the rows collapse to. The copilot is told to reject a
        # figure it cannot check, and without the probe id it could not check any
        # of those -- the first live entry was escalated over exactly such a
        # count (claimed six re-evaluations, actual three).
        "rows": [{"evaluation": r.get("evaluation", ""),
                  "probe": r.get("probe", ""),
                  "when": r.get("when", ""),
                  "config": r["config_label"],
                  "verdict": r.get("verdict", "-"),
                  "bar": r["prereg"]["bar"],
                  "direction": r["prereg"]["direction"],
                  "claim": r.get("claim", "unregistered"),
                  "recorded": bool(r.get("recorded")),
                  "handling": r.get("handling", "unrecorded"),
                  "rejections": r.get("rejections", 0),
                  "escalation_latest": r.get("escalation_latest", ""),
                  # PROJECTED BECAUSE THE RETRY TEMPLATE CITES THEM. They were
                  # decoded into `prereg` all along and never reached the JSON,
                  # so a retry entry had no citable source for its own
                  # pre-registration -- which is the exact rejection it must
                  # avoid.
                  "config_digest": r["prereg"]["cfgh"],
                  "vs": r["prereg"]["vs"],
                  "tol": r["prereg"]["tol"],
                  "hypothesis": r["prereg"]["hypothesis"],
                  "metrics": r.get("metrics") or {},
                  "passed": r.get("passed") or {}}
                 for r in entry["rows"]],
    }


# `yes`/`NO` could not say "rejected twice and not to be offered again", which
# is the whole reason `handling` exists (see `build()`).
#
# `.get(h, f"?{h}?")`, NOT `[h]`, AND NOT `.get(h, "?")` EITHER. This report is
# what a human reads when the loop is stuck -- the diagnostic tool for exactly
# the moment something has gone wrong -- and `render()` has no surrounding
# try/except, so a `KeyError` here does not fail loudly at one cell, it kills
# the WHOLE report: no table, no health summary, nothing on stdout, right when
# the reader needed it most. A bad cell must cost the reader one odd cell, not
# the report. The neighbouring frontier cell a few lines below already makes
# this call -- `{True: "yes", False: "NO"}.get(best["passed"], "?")` -- and
# this follows it, except the fallback carries the value (`?weird?`, not bare
# `?`) so a reader or a `grep '?'` can tell WHICH state was unmodeled.
# `handling` is assigned in `build()` from a closed set of three literals, so
# reaching this fallback at all means a later task added a fourth state and
# forgot to teach this map about it -- that is a real bug, but the fix is to
# extend the dict, not to make this line able to take the report down.
_HANDLING_CELL = {"recorded": "yes", "unrecorded": "NO", "retired": "retired"}


def _named_files(directory, noun, names):
    """`"N {noun} in {directory}: a.md, b.md, c.md, and 4 more"`.

    NAMES A PROBLEM RATHER THAN COUNTING IT -- the whole point of Important-2:
    `"1 file(s), go find it"` sent an operator to `ls` the directory or
    hand-diff every escalation against `escalation-kinds.md` to learn which
    one. `escalation_targets` now returns the basenames for exactly this.

    CAPPED AT 5, SORTED, because the fix for "you don't know which file" must
    not become "the report is now one line per file in a large backlog" --
    that is a new way to make the report unreadable, not a smaller version of
    the old failure. The directory is stated ONCE, here, rather than once per
    name, because the names are bare basenames precisely so this is the only
    place their location needs to be said.
    """
    shown = sorted(names)[:5]
    tail = f", and {len(names) - len(shown)} more" if len(names) > 5 else ""
    return f"{len(names)} {noun} in {directory}: " + ", ".join(shown) + tail


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
               " id; an escalated entry does not count, and a RETIRED row is"
               " excluded from this count too -- it is no longer offered, so"
               " it must not keep inflating the number that used to make it"
               " look like unfinished work forever.")
    if h["unrecorded_runs"]:
        out.append("")
        out.append("| run | config | claim | needs writing up |")
        out.append("|---|---|---|---|")
        for entry in report["series"]:
            for r in entry["rows"]:
                # THE LEADER'S SHORTLIST. Filtering on `recorded` alone let a
                # retired row -- written up twice, rejected twice -- keep
                # appearing here as "needs writing up: yes", which is the
                # 2026-09-04 livelock all over again. Only `unrecorded` rows
                # are still offered; `retired` gets its own visible line
                # below instead.
                if r.get("handling") != "unrecorded":
                    continue
                out.append(f"| {r['evaluation']} | {r['config']} |"
                           f" {r['claim']} | yes |")
    if h["retired_runs"]:
        out.append("")
        for entry in report["series"]:
            for r in entry["rows"]:
                if r.get("handling") != "retired":
                    continue
                # NOT `RETIRED UNRECORDED:` -- that reused the exact word
                # `Unrecorded scored runs:` (and action 1's "a finished run is
                # unrecorded") use for the bucket the leader is supposed to
                # act on, which is the ambiguity `tick-prompt.md` now has to
                # spend a paragraph overriding. The label says only `RETIRED:`
                # -- the fact that it was never written up moves into the
                # sentence instead of the header, so cold reading does not
                # mistake it for "dealt with".
                #
                # PATH CONVENTION: journal-relative everywhere in this
                # sentence, matching what `escalation_latest` already stores
                # (`escalations/<name>`) -- so the revival instruction below
                # is `waivers/<stamp>.md`, not `journal/waivers/<stamp>.md`.
                #
                # `escalation_latest` IS ALWAYS SET HERE, not defaulted: a row
                # only reaches `handling == "retired"` by way of `rejections
                # >= limit` in `build()`, and a counted rejection is exactly
                # what populates `latest` in `escalation_targets` -- there is
                # no path that retires a row without also recording where its
                # newest counted rejection lives. So there is no fallback text
                # to write or test for; asserting one would pin a case this
                # file cannot reach.
                out.append(
                    f"RETIRED: {r['evaluation']} — {r['rejections']}"
                    " rejections, never written up, latest"
                    f" {r['escalation_latest']}. Not selectable; revive with a"
                    " committed waivers/<stamp>.md carrying"
                    f" `**Waiver:** {r['evaluation']}`.")
    if h["unparseable_waivers"]:
        out.append("")
        out.append("UNPARSEABLE WAIVERS " + _named_files(
                   "waivers/", "file(s)",
                   report["problems"]["unparseable_waivers"]) +
                   " -- no `<stamp>` filename prefix, so they revive NOTHING."
                   " Rename them `YYYYMMDDTHHMMSSZ.md` and commit.")
    if h["misdated_waivers"]:
        out.append("")
        out.append("MISDATED WAIVERS " + _named_files(
                   "waivers/", "file(s)",
                   report["problems"]["misdated_waivers"]) +
                   f" -- stamped more than {_WAIVER_LEAD.days} day(s) ahead of"
                   " the journal's newest committed entry, or naming no real"
                   " instant, so they revive NOTHING. A typo'd year or a"
                   " skewed clock; re-stamp `YYYYMMDDTHHMMSSZ` and commit.")
    if h["unclassified_escalations"]:
        out.append("")
        out.append("UNCLASSIFIED ESCALATIONS " + _named_files(
                   "escalations/", "escalation(s)",
                   report["problems"]["unclassified"]) +
                   " -- no `Escalation kind:` line, so they are NOT counted"
                   " toward retirement. Classify them in"
                   " journal/escalation-kinds.md.")

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
        if not entry["ordered"]:
            # LOUD, because a frontier that cannot order its metrics is not a
            # frontier. The contract is what says whether `measured` is a raw
            # metric or an improvement delta, so without it "best" is a guess.
            out.append("")
            out.append("WARNING this series' contract could not be read, so no"
                       " metric can be ordered. The rows below are unranked and"
                       " every claim on them is unjudgeable.")
        out.append("")
        out.append("| bar | best | config | passed |")
        out.append("|---|---|---|---|")
        for name in sorted(entry["frontier"]):
            best = entry["frontier"][name]
            mark = {True: "yes", False: "NO"}.get(best["passed"], "?")
            note = {"band": " (band: first seen)",
                    None: " (unordered: first seen)"}.get(
                        entry["ranks"].get(name), "")
            # REPORTED, NOT GATED. Without this a `NO` on a report row reads as
            # a failed gate, which is the reading the role exists to prevent.
            gates = set(entry.get("gates") or ())
            if gates and name not in gates:
                note += " (report)"
            out.append(f"| {name} | {best['value']:.4g}{note} |"
                       f" {best['config']} | {mark} |")
        out.append("")
        out.append("| when | config | verdict | claimed | outcome | written up |")
        out.append("|---|---|---|---|---|---|")
        for row in entry["rows"]:
            claimed = f"{row['direction']} {row['bar']}" if row["bar"] else "—"
            # `_HANDLING_CELL` REPLACES `'yes' if row['recorded'] else 'NO'`:
            # those two values could not say "rejected twice and not to be
            # offered again", which is the whole reason `handling` exists. See
            # the dict's own comment for why the lookup is `.get(h, f"?{h}?")`
            # rather than `[h]`.
            handling = row.get("handling", "unrecorded")
            out.append(f"| {row['when']} | {row['config']} |"
                       f" {row['verdict']} | {claimed} | {row['claim']} |"
                       f" {_HANDLING_CELL.get(handling, f'?{handling}?')} |")
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

    # RESOLVED ONCE, HERE, THE SAME WAY `journaled`/`escalations`/`problems`
    # ARE: `build()` takes the world as arguments rather than reading it, so a
    # bad `QF_FRONTIER_RETIRE_AFTER` is caught at this boundary and reported
    # the same controlled way as bad stdin, rather than as an uncaught
    # `FrontierError` traceback.
    try:
        retire_after_n = retire_after()
    except FrontierError as e:
        print(str(e), file=sys.stderr)
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
    # NO --journal MEANS NOTHING IS RECORDED AND NOTHING IS RETIRED, which
    # makes the report noisy rather than wrong, in both directions.
    escalations, problems = (escalation_targets(journal) if journal
                             else ({}, {}))
    report = build(rows, extracts, contracts, journaled=journaled,
                   escalations=escalations, problems=problems,
                   retire_after_n=retire_after_n)
    if "--json" in argv:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
