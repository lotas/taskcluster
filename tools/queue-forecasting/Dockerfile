FROM node:24.14.0-slim

RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy workspace config and lockfile
COPY package.json yarn.lock .yarnrc.yml ./
COPY .yarn .yarn/

# Copy only the workspaces needed by queue-forecasting
COPY libraries/pulse libraries/pulse/
COPY libraries/monitor libraries/monitor/
COPY clients/client clients/client/
COPY tools/queue-forecasting tools/queue-forecasting/

# Rewrite root package.json to only reference copied workspaces.
# The monorepo's lib-pulse, lib-monitor, and lib-config don't declare their
# own dependencies — they rely on hoisting from the root package.json.
# `yarn workspaces focus` can't resolve these, so we use a full install
# scoped to just the workspaces we need.
RUN jq '.workspaces = ["libraries/pulse","libraries/monitor","clients/client","tools/queue-forecasting"]' \
    package.json > package.json.tmp && mv package.json.tmp package.json

RUN corepack enable && yarn install

WORKDIR /app/tools/queue-forecasting
