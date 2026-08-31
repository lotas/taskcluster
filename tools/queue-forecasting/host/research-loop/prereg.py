#!/usr/bin/env python3
"""The pre-registration, and the ONE place its format is defined.

WHY A PRE-REGISTRATION AT ALL. An unattended agent runs an order of magnitude
more experiments than a human does, and the failure that scales with volume is
not a bad experiment -- it is a good experiment described afterwards. Run thirty
configs, and one of them clears a bar by chance; write the story after seeing
which, and the story is always available. So the claim has to exist before the
number does.

WHY IT LIVES IN THE PROBE'S `note` AND NOT IN A NEW STORE. The note is already
exactly what a pre-registration needs and nothing else here is:

  * it is written at SUBMIT time, so it cannot be authored after the result --
    that is not a policy, it is the order the operations happen in;
  * `spec.py` puts it in `spec_json`, which the store hash-chains, so the agent
    cannot revise it later even though the agent wrote it;
  * `results.sh` already joins it onto every scored row, so nothing new has to
    be read to see it.

A file in `qf-research` would have none of those properties: the agent owns that
repository, and a record its author can rewrite is not a record.

THE COST is a 500-character printable-ASCII budget (`spec.py:34`), which is why
`hypothesis` is last and is the only field that gets truncated -- losing the tail
of a sentence is legible, losing the bar being claimed is not.

WHAT THIS DELIBERATELY DOES NOT DO: judge. Whether the pre-registered bar
actually moved is `frontier.py`'s question, and it needs the scoreboard to
answer it. This module only writes the claim down and reads it back.
"""
from __future__ import annotations

import math

# The contract's metric names, from `contracts/wait_time.v1.json`. A closed set
# on purpose: `bar=tail` or `bar=p90` would each read fine and neither would
# ever match a scoreboard key, so the pre-registration would be unfalsifiable
# in precisely the way this file exists to prevent.
BARS = ("mae", "within_2x", "p90_coverage", "p90_miss_tail")

# `improve` claims the bar moves in the contract's good direction. `hold` claims
# it does NOT move -- which is a real hypothesis here and not a hedge: the qctx
# result trades tail coverage for central accuracy, so "this change does not cost
# the tail" is the interesting claim about half the queue's remaining entries.
DIRECTIONS = ("improve", "hold")

# MIRRORS `spec.py:34`. Duplicated as a number rather than imported because
# `shared/README.md` reserves the imported-by-both-domains path for trusted code
# and this is not trusted code. `test_prereg.py` asserts the two agree, so the
# duplication is checked rather than assumed.
NOTE_MAX = 500

_KEYS = ("cfg", "cfgh", "bar", "dir", "vs", "tol", "ref", "hyp")
_SEP = " | "

# How much of the config digest goes in the note. Twelve hex characters is the
# same width the dispatcher uses in a run id, and it is identity for this purpose
# rather than a security boundary -- the question is "is this the same file as
# last cohort", asked of files the agent itself wrote minutes apart.
CFGH_LEN = 12


class PreregError(Exception):
    """A pre-registration that cannot be written as claimed."""


def _clean(text: str) -> str:
    """Collapse to the printable-ASCII, newline-free subset the note allows.

    Substitution and not refusal, for this field only: an agent writing a
    hypothesis will eventually type an em dash, and refusing the whole
    experiment over a punctuation mark would put an operator back in the loop
    for a reason that has nothing to do with the experiment. The structured
    fields do NOT get this treatment -- they are validated and refused, because
    a silently rewritten bar name is a silently different claim.
    """
    out = []
    for ch in (text or "").replace("\t", " "):
        if ch in "\r\n":
            out.append(" ")
        elif "\x20" <= ch <= "\x7e":
            out.append(ch)
        else:
            out.append("?")
    # Runs of spaces come from the substitutions above, and they spend the
    # budget that the hypothesis's own words need.
    return " ".join("".join(out).split())


def _structured(value: str, field: str) -> str:
    """A structured field's value, REFUSED rather than cleaned if it could forge.

    The separator is the only way to create a new `key=value` part, so a
    structured value carrying one can inject a field that appears BEFORE the
    genuine one -- and `decode` takes the first occurrence of each key, so the
    injected value wins. A config named `configs/a | cfgh=deadbeefdead` shadowed
    the real digest exactly this way, which is enough to make two different
    files confirm each other.

    Refused and not escaped: no config path, run id or digest in this system
    contains a pipe, so a value that does is a mistake or an attack and both are
    better stopped than rewritten into something the caller did not ask for.
    (The HYPOTHESIS is different and may contain anything -- `decode` stops
    parsing at the `hyp=` marker, so it cannot forge a field.)
    """
    text = _clean(value)
    if "|" in text:
        raise PreregError(
            f"{field} cannot contain a pipe: {value!r}. The note is a"
            " `key=value | key=value` string, so a pipe inside a field would"
            " forge another field.")
    return text


def config_digest(path: str) -> str:
    """A content digest for the config file, so identity is not a filename.

    WHY. A config label alone is a PATH, and the agent owns the checkout the path
    points into. Nothing stops it editing `configs/x.yaml` between two cohorts and
    having two different models confirmed under one name -- which is the exact
    shape of a false confirmation, because the second cohort was supposed to be
    the check on the first.

    Hashes the raw bytes, not a parsed and re-serialised form: a comment change
    is a different file and should read as one. Being too sensitive here costs a
    re-run; being too loose costs a wrong CONFIRMED.
    """
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:CFGH_LEN]


def encode(config: str, bar: str, direction: str, hypothesis: str,
           vs: str = "", cfgh: str = "", tol: float = 0.0,
           reference: bool = False) -> str:
    """The note string for a pre-registered run.

    `cfg=` stays FIRST and the separator stays `" | "` because `results.py`'s
    `_split_note` already parses that shape to label its config column. A new
    format here would blank that column for every future row.
    """
    if bar not in BARS:
        raise PreregError(
            f"bar must be one of {', '.join(BARS)}, got {bar!r}."
            " These are the contract's metric names; a bar that is not one of"
            " them can never be checked against a scoreboard.")
    if direction not in DIRECTIONS:
        raise PreregError(
            f"dir must be one of {', '.join(DIRECTIONS)}, got {direction!r}")
    config = _structured(config, "the config path")
    if not config:
        raise PreregError("a pre-registration needs the config it is about")
    hypothesis = _clean(hypothesis)
    if not hypothesis:
        raise PreregError(
            "a pre-registration needs a hypothesis: the bar alone records what"
            " was measured, not what was believed, and 'what was believed' is"
            " the only part that cannot be reconstructed afterwards.")
    if vs:
        vs = _structured(vs, "vs")
    if reference and vs:
        raise PreregError(
            "a reference run has nothing to be judged against, so --vs and"
            " --reference-run are mutually exclusive")
    if not (vs or reference):
        raise PreregError(
            "a pre-registration needs --vs <run id in the same series>, or"
            " --reference-run if this is the first run of a new series."
            " Without one the claim cannot come out false, and the run costs"
            " a full training cycle to produce a number nobody can check.")
    try:
        tol = float(tol or 0.0)
    except (TypeError, ValueError):
        raise PreregError(f"tol must be a number, got {tol!r}")
    if not math.isfinite(tol):
        # `inf` MAKES EVERY NUMERIC `hold` SUCCEED, and `nan` makes every
        # comparison against it false. Either one is a claim that cannot come out
        # wrong, which is the single thing a pre-registration must never be.
        raise PreregError(
            f"tol must be finite, got {tol!r}: a non-finite tolerance is a"
            " claim that cannot be refuted")
    if tol < 0:
        # A NEGATIVE TOLERANCE WOULD INVERT THE CLAIM: `hold` with tol=-0.05
        # would demand a 5-point improvement while reading as "no change".
        raise PreregError("tol cannot be negative; it is how much WORSE the"
                          " metric may get and still count as held")

    fixed = [f"cfg={config}", f"bar={bar}", f"dir={direction}"]
    if cfgh:
        fixed.append(f"cfgh={_structured(cfgh, 'cfgh')}")
    if vs:
        fixed.append(f"vs={vs}")
    if reference:
        fixed.append("ref=1")
    if tol:
        # OMITTED WHEN ZERO, so the common case reads clean and a note carrying
        # `tol=` is a note where slack was deliberately claimed.
        fixed.append(f"tol={tol:g}")
    head = _SEP.join(fixed)

    # The hypothesis gets whatever is left. `- 1` for the ellipsis it may need.
    room = NOTE_MAX - len(head) - len(_SEP) - len("hyp=")
    if room < 8:
        # Reachable only with a pathological config path; the structured fields
        # are worth more than the prose, so this refuses rather than dropping
        # the hypothesis and producing a note that LOOKS pre-registered.
        raise PreregError(
            f"no room left for a hypothesis: {head!r} already spends"
            f" {len(head)} of {NOTE_MAX} characters")
    if len(hypothesis) > room:
        # `...` and not an ellipsis character: the note is printable ASCII only,
        # and a `?` substituted for `…` reads as a typo rather than as elision.
        hypothesis = hypothesis[: room - 3] + "..."
    return head + _SEP + f"hyp={hypothesis}"


def decode(note: str) -> dict:
    """Read a note back. Never raises -- an unparseable note is a legacy note.

    `registered` is the field that matters to a caller: every row scored before
    this module existed has a free-text note, and treating those as violations
    would make the frontier's first report a wall of false alarms. They are not
    violations, they are history.
    """
    out = {"config": "", "cfgh": "", "bar": "", "direction": "", "vs": "",
           "tol": 0.0, "tol_error": "", "reference": False,
           "hypothesis": "", "registered": False, "raw": note or ""}
    text = (note or "").strip()
    if not text:
        return out

    # THE HYPOTHESIS IS EVERYTHING AFTER `hyp=`, and no key parsing happens in
    # it. This is a security property, not a convenience.
    #
    # The hypothesis is agent-authored free text. Parsing it for `key=value`
    # meant a hypothesis could SHADOW any structured field that `encode` had
    # omitted -- and `encode` omits `tol` when it is zero and `vs` on a reference
    # run. So `--note "dropping capacity is safe | tol=5"` produced a note whose
    # decoded tolerance was 5 against a pre-registered 0, which is precisely the
    # unrefutable-claim hole the tolerance validation was added to close,
    # reopened through the one field the agent writes freely.
    #
    # Splitting here also means the hypothesis round-trips VERBATIM, separators
    # and all, instead of needing to be sanitised.
    head, marker, hypothesis = text.partition(_SEP + "hyp=")
    if not marker:
        # No `hyp=` at all: a legacy or hand-typed note. Parse the whole thing,
        # which is what it was before this format existed.
        head, hypothesis = text, ""

    found = {}
    free = []
    for part in head.split(_SEP):
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and key in _KEYS and key != "hyp" and key not in found:
            found[key] = value.strip()
        else:
            # Free text: a hand-typed note, or the remainder of a `hyp=` value
            # that itself contained the separator. APPENDED, never dropped --
            # `encode` puts the hypothesis last precisely so its own text can
            # contain anything, and a reader that discarded the overflow would
            # silently shorten the only field a human wrote.
            free.append(part)

    out["config"] = found.get("cfg", "")
    out["cfgh"] = found.get("cfgh", "")
    out["bar"] = found.get("bar", "")
    out["direction"] = found.get("dir", "")
    out["vs"] = found.get("vs", "")
    # THE FIRST RUN IN A NEW SERIES has nothing to be judged against, and that is
    # a real situation rather than a loophole -- so it is declared, in the
    # immutable note, instead of being inferred from a missing `vs`.
    out["reference"] = found.get("ref", "") == "1"
    # A MALFORMED TOLERANCE INVALIDATES THE WHOLE REGISTRATION rather than being
    # coerced to something safe. Coercion was the bug: `abs()` turned an injected
    # `tol=-10` into a permissive `+10`, and falling back to 0 on `tol=inf` would
    # have quietly rewritten a claim that cannot be refuted into a strict one --
    # both of which report a prediction nobody made. If the field is present and
    # unreadable, the note is not a pre-registration.
    out["tol"] = 0.0
    if "tol" in found:
        try:
            value = float(found["tol"])
        except (TypeError, ValueError):
            out["tol_error"] = f"unreadable tol {found['tol']!r}"
        else:
            if not math.isfinite(value):
                out["tol_error"] = f"non-finite tol {found['tol']!r}"
            elif value < 0:
                out["tol_error"] = f"negative tol {found['tol']!r}"
            else:
                out["tol"] = value
    # `hypothesis` is the verbatim tail when there was a `hyp=` marker; otherwise
    # it is whatever free text the note carried, which is still the best
    # description of a hand-typed run.
    out["hypothesis"] = hypothesis if marker else " ".join(free)
    # REQUIRES ALL THREE. A note carrying `bar=` but no `dir=` records a metric
    # to look at and no claim about it, which is not a prediction and must not
    # be counted as one.
    out["registered"] = bool(out["config"] and out["bar"] in BARS
                             and out["direction"] in DIRECTIONS
                             and not out.get("tol_error"))
    return out


def is_valid_note(note: str) -> bool:
    """Would `spec.py` accept this string? Checked here so a refusal happens
    before the push, not after twenty minutes of training."""
    return (isinstance(note, str) and len(note) <= NOTE_MAX
            and all("\x20" <= ch <= "\x7e" for ch in note))
