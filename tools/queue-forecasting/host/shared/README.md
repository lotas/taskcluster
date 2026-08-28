# `host/shared` — trusted code both privilege domains import

One module today: `extract_spec.py`, the closed-world extraction request.

**Why it is here and not in `dispatcher/`.** D16 requires that BOTH `qfd` and the
extractor validate an extraction request — `qfd` so a bad request is refused
cheaply and legibly at submit time, the extractor because a caller is a caller and
the point of a separate privilege domain is that the caller's diligence is not
part of the guarantee. Two validators must agree, so there is one module.

It was originally in `dispatcher/`, with the extractor reaching into that
directory. That worked in the tests, which insert the path, and would have failed
at the first start of the real service: `/usr/bin/python3` cannot import
`extract_spec` from a directory nobody put on the path. **A dependency that only
resolves under the test harness is not a dependency that resolves.**

Both units put this directory on `PYTHONPATH`. The alternative — pointing the
extractor's `PYTHONPATH` at `dispatcher/` — would have made every dispatcher
module importable by the extractor, including `qfd.py` and `store.py`, which is a
larger surface than the one thing they share.

Nothing here may import from `dispatcher/` or `extractor/`. The dependency runs
one way only, and `shared` is the bottom.
