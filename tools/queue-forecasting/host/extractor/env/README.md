# The extractor's host environment

`uv.lock` IS NOT IN THIS REPOSITORY, and `phase2b-setup.sh install` now REFUSES
without it.

That refusal replaced a warning, and the reason is that the warning did not work:
the previous version generated a lock on the host and printed "now commit it",
that reminder was read, and the lock is still absent. **A warning that has been
ignored once is documentation; a refusal is a control.** The escape hatch is
`ALLOW_UNLOCKED_ENV=1`, which exists so the first lock can come into being at
all.

`uv` is not available in the development environment, so the lock cannot be
generated here. `phase2b-setup.sh install` runs `uv sync --frozen` when a lock is
present, and refuses when it is not. Until one is committed, two hosts installed
a week apart can get different versions of pyarrow.

That matters less than it would elsewhere and it still matters. Less, because
published extracts are immutable (D20), so a pyarrow upgrade cannot change an
extract that already exists — only the bytes of future ones, which get their own
`extract_hash`. Still, because "two hosts, two versions" is how a difference
nobody can explain gets into a comparison.
