"""The identity of a published extract's manifest. Phase 2c review fix.

`extract_spec.py` owns the identity of the REQUEST (`request_hash`, which names
the extract and is what D20 keys reuse on). This owns the identity of the
MANIFEST -- `extract_hash`, which every member of a comparison must share.
`baseline.py` is the exact same shape for a promoted baseline set, and the three
sit together in `shared/` for the same reason: several privilege domains read
them and none may depend on another.

WHY THIS IS ITS OWN MODULE AND NOT A FUNCTION IN THE EXTRACTOR. It was one:
`extractor._canonical` plus one `hashlib.sha256` call at the point of writing.
The extractor is the only thing that WRITES a manifest, so that looked fine --
and it meant nothing could CHECK one. A review found the consequence: the
evaluator recorded `extract_hash` into every verdict without ever recomputing
it, and the test fixture that stood in for a real extract computed the field
with `json.dumps(..., sort_keys=True)` -- default separators, so spaces after
every delimiter, so different bytes and a different hash. The fixture was wrong
and passed, because the only code that could have noticed did not look.

A content key with one implementation and no verifier is not a content key. It is
a field.
"""
from __future__ import annotations

import hashlib
import json

# The key that carries the digest, excluded from the digest -- the same
# self-describing shape as `baseline_hash` and `contract_hash`, verified the same
# way: recompute and compare, never trust the field.
HASH_FIELD = "extract_hash"

MANIFEST = "MANIFEST.json"


class ExtractManifestError(ValueError):
    """A manifest that must not be used to attribute anything."""


def canonical(manifest):
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8.

    NO WHITESPACE IS LOAD-BEARING. `json.dumps(obj, sort_keys=True)` -- without
    `separators` -- emits `", "` and `": "`, which is a different byte string and
    therefore a different hash. That exact substitution is what made the test
    fixture wrong.
    """
    body = {k: v for k, v in manifest.items() if k != HASH_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def extract_hash(manifest):
    return hashlib.sha256(canonical(manifest)).hexdigest()


def verify(manifest):
    """The declared `extract_hash`, or `ExtractManifestError`.

    Refuses a manifest that declares nothing as well as one that declares
    something wrong: an absent content key is not a weaker claim than a
    mismatched one, it is the same claim with nothing behind it.
    """
    if not isinstance(manifest, dict):
        raise ExtractManifestError(
            f"an extract manifest must be an object, got"
            f" {type(manifest).__name__}")
    declared = manifest.get(HASH_FIELD)
    if not isinstance(declared, str) or not declared:
        raise ExtractManifestError(
            f"the extract manifest declares no {HASH_FIELD}, so nothing it says"
            f" can be attributed to a particular extract")
    try:
        recomputed = extract_hash(manifest)
    except (TypeError, ValueError) as e:
        raise ExtractManifestError(
            f"the extract manifest cannot be canonicalised ({e}): a body that"
            f" does not hash is not an identity") from None
    if recomputed != declared:
        raise ExtractManifestError(
            f"the extract manifest declares {HASH_FIELD} {declared[:12]} but its"
            f" body hashes to {recomputed[:12]}: it has been edited since it was"
            f" written, and a content key that does not match its content"
            f" records nothing")
    return declared
