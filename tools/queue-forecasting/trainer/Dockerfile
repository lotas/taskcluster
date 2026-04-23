FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# LightGBM requires the OpenMP runtime; the -slim base image omits it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/trainer
COPY tools/queue-forecasting/trainer/pyproject.toml \
     tools/queue-forecasting/trainer/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY tools/queue-forecasting/trainer /app/trainer

ENV PYTHONPATH=/app/trainer
