#!/bin/sh
# Waits for Keycloak, then runs the Canonic MCP http daemon in the foreground so it is the
# container's PID 1: it receives SIGTERM directly and a crash ends the container. Logs go
# to stderr (canonic.docker.yaml sets no logging.file), so `docker compose logs canonic`
# shows them.
set -e

# mcp.auth.oauth (proxy mode) fetches Keycloak's OIDC discovery document synchronously
# while constructing the auth provider (fastmcp's OIDCProxy.__init__, "timeout_seconds:
# ... for the OIDC discovery request made during construction"). depends_on only waits
# for the keycloak container to *start*, not for start-dev to finish importing the realm
# and accept connections, so without this wait the daemon's auth-provider construction
# hits connection-refused and the child process exits immediately ("MCP daemon exited
# before becoming ready"). Plain python3 (always present in the base image) avoids
# depending on curl/wget being installed in this image. "localhost" resolves to Keycloak
# here because this container shares its network namespace (network_mode: service:keycloak
# in docker-compose.yml) — see canonic.docker.yaml's issuer_url comment for why.
echo "waiting for Keycloak realm 'canonic' to become ready..."
python3 - <<'PY'
import sys
import time
import urllib.request

url = "http://localhost:8080/realms/canonic/.well-known/openid-configuration"
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status == 200:
                sys.exit(0)
    except OSError:
        pass
    time.sleep(2)
sys.exit(f"keycloak did not become ready at {url} within 120s")
PY

# `--foreground` keeps the daemon in this process (see `canonic mcp start --help`), so
# `exec` hands PID 1 over to it. `uv run` needs pyproject.toml/uv.lock/the synced venv,
# which live in /app (the Dockerfile's WORKDIR), so uv is pointed there via --project.
#
# --no-sync: the image was already synced at build time (uv sync --frozen --no-dev in
# the Dockerfile); without it, `uv run` re-resolves and installs the dev dependency
# group (ruff, mypy, ...) on every container start, which is slow and defeats
# air-gapped/offline use.
exec uv run --project /app --no-sync canonic mcp start \
  --project /data/marketplace \
  --transport http \
  --host 0.0.0.0 \
  --port 7474 \
  --foreground
