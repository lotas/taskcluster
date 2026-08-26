# TRUSTED. Root-owned, read only from the trusted checkout. The copy of
# trainer/Dockerfile that travels with qf-research is ignored entirely
# (auto-research-phase1-design.md §6).
#
# Base pinned by digest: `phase2-setup.sh pin-base` prints the line to paste.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

# LightGBM needs the OpenMP runtime; the -slim base omits it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# The environment must NOT live under /app/trainer: that path is a read-only
# mount of the research worktree at runtime and would shadow .venv (D8).
ENV UV_PROJECT_ENVIRONMENT=/opt/qfenv
WORKDIR /opt/qfbuild
# Only the human-promoted manifests. The build context contains exactly these
# two files plus this Dockerfile -- image.py asserts it.
COPY pyproject.toml uv.lock ./
# --locked asserts lock and manifest agree (--frozen would merely trust the
# lock). --no-install-project keeps a [build-system] table from ever executing.
RUN uv sync --locked --no-install-project

# The in-container identity. gid 10001 is group qfrun on the host, which qfd
# joins so it can hand `out/` over by group (design §4.4).
RUN groupadd -g 10001 qfrun && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin qfrun

ENV PATH=/opt/qfenv/bin:$PATH \
    PYTHONPATH=/app/trainer \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp
WORKDIR /app/trainer
# No ENTRYPOINT: the dispatcher supplies an absolute interpreter path as argv.
# `uv run` at runtime would want to re-resolve, which needs write access and,
# on drift, network -- neither of which the sandbox has.
