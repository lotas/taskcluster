"""Source with comments and string literals removed. Test infrastructure.

WHY THIS EXISTS, AND WHY THE PREVIOUS FIX WAS NOT ENOUGH. Static scans over this
project's own source have matched their own documentation NINE times: a static
scan asserting `force=` is absent matched five prose uses of "force"; a mode glob
quoted in the comment explaining why it was replaced; a suite message quoted in
the test asserting it was gone; and now a module docstring saying "nothing here
writes to trainer/data/models/" matching a scan asserting exactly that.

The sixth instance produced `code_only()`, which strips `#` lines. That fixed the
comment half and left the docstring half, so the ninth instance landed in the
first file whose prohibition was stated in a docstring rather than a comment.
Stripping lines cannot work in general -- a docstring is an expression, not a
line prefix -- so this tokenises.

STRING LITERALS GO TOO, not just docstrings. A scan for `"models/"` should not
match a literal that happens to contain it either: the property being asserted is
almost always "this code does not DO X", and a string is data.

Stdlib-only (`tokenize`), because it is imported by tests in two privilege
domains and `qfd`'s domain is stdlib-only by design (D6).
"""
from __future__ import annotations

import io
import tokenize


def code_only(source):
    """`source` with comments and string literals blanked out.

    Layout is preserved line-for-line so that offsets and `index()`-based slicing
    in a caller still land where the caller expects; removed tokens become
    spaces rather than disappearing.
    """
    lines = source.splitlines(keepends=True)
    blank = [list(line) for line in lines]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file that does not tokenise is returned unchanged rather than
        # silently emptied: an empty result would make every `assertNotIn` pass.
        return source
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = token.start, token.end
        for row in range(srow, erow + 1):
            chars = blank[row - 1]
            lo = scol if row == srow else 0
            hi = ecol if row == erow else len(chars)
            for i in range(lo, min(hi, len(chars))):
                if chars[i] != "\n":
                    chars[i] = " "
    return "".join("".join(chars) for chars in blank)


def shell_code_only(text, comment="#"):
    """The shell equivalent: `text` without whole-line comments.

    Shell has no docstrings and no reliable way to find a quoted string without
    parsing the language, so this stays line-based -- and says so, rather than
    implying it does what `code_only` does.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(comment))
