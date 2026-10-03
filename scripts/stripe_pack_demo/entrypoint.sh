#!/bin/sh
# Installs the "stripe" context pack against the seeded Postgres database, then runs the
# MCP http daemon in the foreground so it stays this container's PID 1. See README.md.
set -e

# `pack add` finds the project by walking up from the working directory and has no
# --project flag, so this directory change is required.
cd /data/stripe-demo

# --repo points at the canonic-packs checkout mounted at /packs (see docker-compose.yml),
# so the demo runs the pack from your working tree, including unmerged branches. A local
# path is used as-is, no git needed. --connection binds the Postgres connection already in
# canonic.yaml, --params-file supplies every other param, --yes skips the confirmation.
# Rerunning on a container restart is safe, install writes every target file again.
echo "installing the stripe context pack..."
uv run --project /app --no-sync canonic pack add stripe \
  --repo /packs \
  --variant postgres \
  --connection stripe_db \
  --params-file pack-params.json \
  --yes

# --no-sync: the image was synced at build time, without it `uv run` re-resolves and
# installs the dev dependency group on every container start.
exec uv run --project /app --no-sync canonic mcp start \
  --project /data/stripe-demo \
  --transport http \
  --host 0.0.0.0 \
  --port 7474 \
  --foreground
