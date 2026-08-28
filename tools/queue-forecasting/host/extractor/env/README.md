# The extractor's host environment

`uv.lock` IS NOT IN THIS REPOSITORY YET, and that is a gap rather than a choice.

`uv` is not available in the development environment, so the lock cannot be
generated here. `phase2b-setup.sh install` runs `uv lock` when it is absent and
`uv sync --frozen` when it is present, and **prints a reminder to commit the
generated lock**. Until it is committed, two hosts installed a week apart can get
different versions of pyarrow.

That matters less than it would elsewhere and it still matters. Less, because
published extracts are immutable (D20), so a pyarrow upgrade cannot change an
extract that already exists — only the bytes of future ones, which get their own
`extract_hash`. Still, because "two hosts, two versions" is how a difference
nobody can explain gets into a comparison.
