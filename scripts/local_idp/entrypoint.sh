#!/bin/sh
# Starts the Canonic MCP http daemon against the marketplace example, then keeps the
# container's PID 1 alive by tailing its log.
#
# `canonic mcp start --transport http` deliberately daemonizes: it spawns a detached
# subprocess.Popen child and returns once that child is ready (see
# canonic/mcp/daemon.py), so it cannot be used as a container's sole foreground
# command — the container would exit right after the "started" message even though the
# actual daemon is still running as an orphaned child. Tailing the log file it already
# writes to (mcp.auth config's logging.file) keeps PID 1 alive without reaching for the
# internal `--_child` flag, which canonic/cli/commands/mcp.py's docstring says not to
# pass by hand.
set -e

# .canonic/ is bind-mounted from the host (docker-compose.yml), so mcp.json (the daemon
# state file canonic/mcp/daemon.py writes on a successful start) survives across
# `docker compose down`/`up` even though each run gets a fresh container and PID
# namespace. status()'s liveness check is a bare os.kill(pid, 0) against *this* container's
# processes, so a stale mcp.json from a previous container incarnation can false-positive
# as "running" if the new container happens to reuse that PID (quite likely — early PIDs
# like 1/40 get reassigned fast in a mostly-empty container). There is no legitimate case
# where a prior container's daemon is still alive once that container is gone, so it's
# always safe to clear this here.
rm -f /data/marketplace/.canonic/mcp.json

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

# canonic.yaml's logging.file (.canonic/canonic.log) is a relative path resolved against
# the process's cwd, not against --project. start_http() (canonic/mcp/daemon.py) spawns the
# detached daemon child via subprocess.Popen without passing cwd=, so the child inherits
# whatever cwd it was launched from — the Dockerfile's WORKDIR (/app) unless we cd first.
# Without this, the child crashes in configure_logging() with FileNotFoundError
# (/app/.canonic/canonic.log doesn't exist) before it can bind the port, which
# _wait_for_daemon_ready() then reports as "MCP daemon exited before becoming ready".
#
# `uv run` itself needs to find pyproject.toml/uv.lock/the synced venv, which live in
# /app (the Dockerfile's WORKDIR), not in /data/marketplace (which has none of those and
# isn't a uv project) — so cwd is changed here, but uv is told where the project actually
# is via --project, instead of `cd`-ing uv itself there too.
cd /data/marketplace

# --no-sync: the image was already synced at build time (uv sync --frozen --no-dev in
# the Dockerfile); without it, `uv run` re-resolves and installs the dev dependency
# group (ruff, mypy, ...) on every container start, which is slow and defeats
# air-gapped/offline use.
uv run --project /app --no-sync canonic mcp start \
  --project /data/marketplace \
  --transport http \
  --host 0.0.0.0 \
  --port 7474

mkdir -p /data/marketplace/.canonic
touch /data/marketplace/.canonic/canonic.log
exec tail -f /data/marketplace/.canonic/canonic.log
