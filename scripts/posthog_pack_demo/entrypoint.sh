#!/bin/sh
# Installs the "posthog" context pack (AMENDMENT-context-packs) against the seeded
# Postgres database, then runs the MCP http daemon in the foreground so it stays this
# container's PID 1 (receives SIGTERM directly, a crash ends the container). See
# README.md for the full picture.
set -e

# `find_project_root()` walks up from cwd looking for canonic.yaml — `pack add` has no
# `--project` flag, unlike `mcp start` below, so this directory change is required, not
# cosmetic.
cd /data/posthog-demo

# --repo is a local path (the canonic-packs checkout bind-mounted at /packs/canonic-packs,
# see docker-compose.yml for why this isn't a --repo <github-url> clone yet). --connection
# binds the one Postgres connection already in canonic.yaml; --params-file supplies every
# other required param non-interactively; --yes skips the write-preview confirmation.
# install_pack (canonic/packs/install.py) writes every target file unconditionally, so
# rerunning this on a container restart is safe — no idempotency guard needed here.
echo "installing the posthog context pack..."
uv run --project /app --no-sync canonic pack add posthog \
  --repo /packs/canonic-packs \
  --variant postgres \
  --connection posthog_db \
  --params-file pack-params.json \
  --yes

# --no-sync: the image was already synced at build time (uv sync --frozen --no-dev in the
# Dockerfile); without it, `uv run` re-resolves and installs the dev dependency group on
# every container start.
exec uv run --project /app --no-sync canonic mcp start \
  --project /data/posthog-demo \
  --transport http \
  --host 0.0.0.0 \
  --port 7474 \
  --foreground
