"""MCP daemon lifecycle: start, stop, status and PID-file management (SPEC E8 §4.2).

State is written to ``.canonic/mcp.json`` in the project root. Two transports:

- **stdio** (default) — the server runs in the foreground; the MCP client manages
  the process lifetime (``canonic mcp start`` blocks until the client disconnects).
- **http** — a uvicorn-backed HTTP daemon runs detached in the background; the PID
  file tracks the process so ``canonic mcp stop/status`` work. Network-reachable, so
  it requires an auth provider — a bearer token (AMENDMENT-remote-mcp-transport.md),
  OAuth 2.1 (AMENDMENT-oauth-mcp-auth.md), or both — ``stdio`` needs none.

The background daemon is spawned via ``subprocess.Popen`` (fork+exec into a fresh
``python -m canonic`` process), not a bare ``os.fork()``. Forking a multi-threaded
interpreter and continuing to run Python in the child without an intervening ``exec()``
is unsafe on macOS: system frameworks the child later touches (DNS resolution via
Network.framework, TLS, ``os_log``-backed logging) may hold locks that belonged to
threads which no longer exist post-fork, so any later call into them from the child
can deadlock or crash with SIGSEGV — this is exactly what produced crash reports where
a background asyncio thread died inside ``getaddrinfo`` with "crashed on child side of
fork pre-exec". ``exec()`` replaces the process image and discards that stale state
before any unsafe code runs, so re-launching via subprocess avoids the hazard entirely.

Version compatibility: the running Canonic package version is stamped in the state
file so a mismatch is surfaced immediately (SPEC §4.2 AC2).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations
from typing import TYPE_CHECKING

from canonic import __version__ as CANONIC_VERSION

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import AuthProvider

    from canonic.contracts.principal import Principal

__all__ = [
    "DaemonState",
    "DaemonStatus",
    "read_state",
    "serve_http_foreground",
    "start_http",
    "start_stdio",
    "status",
    "stop",
]

_STATE_FILE = ".canonic/mcp.json"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7474

#: How long to wait for the spawned child to start accepting connections before
#: giving up (SPEC E8 §4.2). Generous on purpose: the child re-imports canonic from
#: scratch, including optional heavy/network-touching deps (e.g. a cost-map fetch
#: that falls back locally after its own multi-second timeout) before uvicorn binds.
_READY_TIMEOUT = 15.0
_READY_POLL_INTERVAL = 0.1


@dataclass
class DaemonState:
    """Persisted daemon metadata (written to ``.canonic/mcp.json``)."""

    pid: int
    version: str
    transport: str
    host: str | None
    port: int | None
    started_at: str
    auth_enabled: bool = False
    #: Active auth mechanisms, e.g. ``["token", "oauth-proxy"]`` — see
    #: ``canonic.mcp.auth.describe_auth_mechanisms``. Defaults to ``[]`` so a state
    #: file written before this field existed still parses (``read_state`` does
    #: ``DaemonState(**data)``).
    auth_mechanisms: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass
class DaemonStatus:
    """Runtime status as surfaced by ``canonic mcp status``."""

    running: bool
    pid: int | None = None
    version: str | None = None
    transport: str | None = None
    host: str | None = None
    port: int | None = None
    started_at: str | None = None
    version_mismatch: bool = False
    current_version: str | None = None
    auth_enabled: bool = False
    auth_mechanisms: list[str] = field(default_factory=list)


def _state_path(project_root: Path) -> Path:
    return project_root / _STATE_FILE


def _write_state(project_root: Path, state: DaemonState) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json())


def _remove_state(project_root: Path) -> None:
    path = _state_path(project_root)
    if path.exists():
        path.unlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_state(project_root: Path) -> DaemonState | None:
    """Read and parse ``.canonic/mcp.json``; returns ``None`` when absent."""
    path = _state_path(project_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return DaemonState(**data)
    except Exception:  # noqa: BLE001 — malformed state file treated as absent
        return None


def status(project_root: Path) -> DaemonStatus:
    """Check whether the daemon is running and report its state."""
    state = read_state(project_root)
    if state is None:
        return DaemonStatus(running=False)

    if not _pid_alive(state.pid):
        # Stale state file — clean it up.
        _remove_state(project_root)
        return DaemonStatus(running=False)

    current = CANONIC_VERSION
    mismatch = state.version != current
    return DaemonStatus(
        running=True,
        pid=state.pid,
        version=state.version,
        transport=state.transport,
        host=state.host,
        port=state.port,
        started_at=state.started_at,
        version_mismatch=mismatch,
        current_version=current,
        auth_enabled=state.auth_enabled,
        auth_mechanisms=state.auth_mechanisms,
    )


def stop(project_root: Path) -> bool:
    """Send SIGTERM to the daemon process and remove the state file.

    Returns ``True`` when the daemon was running, ``False`` when it was already
    stopped (no error raised in either case).
    """
    state = read_state(project_root)
    if state is None:
        return False
    if not _pid_alive(state.pid):
        _remove_state(project_root)
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(state.pid, signal.SIGTERM)
    _remove_state(project_root)
    return True


def _wait_for_daemon_ready(
    proc: subprocess.Popen[bytes],
    host: str,
    port: int,
    log_path: Path,
    *,
    timeout: float = _READY_TIMEOUT,
) -> None:
    """Block until the spawned child is confirmed accepting connections on ``(host, port)``.

    A fixed short sleep followed by a bare ``proc.poll()`` check (the previous
    approach) only catches a crash that happens to land within that window — a
    child that is still importing/initializing, or one that fails to bind moments
    later (e.g. a stale daemon already holding the port), is misreported as started.
    This polls both the child's liveness and a real TCP connect to the port it should
    be listening on, so "started" means "actually reachable," not "hasn't crashed yet."

    Raises :class:`RuntimeError` if the child exits before becoming ready, or if it
    hasn't started accepting connections within ``timeout`` seconds — in the timeout
    case the child is killed first, so a slow-starting process that eventually does
    bind never ends up as an untracked orphan (no state file was written for it).
    """
    # A bind-all host isn't itself a valid connect target; probe loopback instead,
    # matching how a client on the same machine would actually reach it.
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # noqa: S104

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"MCP daemon exited before becoming ready — check {log_path} for details"
            )
        try:
            with socket.create_connection((check_host, port), timeout=_READY_POLL_INTERVAL):
                return
        except OSError:
            time.sleep(_READY_POLL_INTERVAL)

    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    raise RuntimeError(
        f"MCP daemon did not start accepting connections on {host}:{port} within "
        f"{timeout:.0f}s — check {log_path} for details"
    )


def start_stdio(
    service: object,
    project_root: Path,
    *,
    suggestions: bool = False,
    principal: Principal | None = None,
) -> None:
    """Run the MCP server in stdio transport mode (foreground, blocks).

    The MCP client (e.g. Claude Code) manages the process lifetime; this function
    returns when the client disconnects or the process is killed.

    ``stdio`` has no per-request auth, so a project with a tenancy policy configured
    has no way to derive a principal for anything served over this transport. Per
    SPEC-E12 §5 (S13 AC3), refuse to start rather than serve unscoped: the caller must
    pass ``principal`` (bound from ``canonic mcp start --tenant <id>``) to fix one for the
    whole session — threaded into ``build_server`` as ``session_principal`` so every tool
    call in this session scopes to it.
    """
    from canonic.config import load_config
    from canonic.log import _effective_log_params, configure_logging
    from canonic.mcp.server import build_server

    try:
        cfg = load_config(project_root / "canonic.yaml")
        level, file, format = _effective_log_params(
            cfg.logging.level, cfg.logging.file, cfg.logging.format
        )
    except Exception:
        level, file, format = _effective_log_params("WARNING", None)
    configure_logging(level=level, file=file, format=format)

    _check_version_on_start(project_root)
    if service.resolver.tenancy_enabled and principal is None:  # type: ignore[attr-defined]
        raise RuntimeError(
            "a tenancy policy is configured for this project, and stdio transport has no "
            "per-request auth to derive a principal from — pass `canonic mcp start "
            "--tenant <id>` to bind one for this session (SPEC-E12 §5)"
        )
    mcp = build_server(service, suggestions=suggestions, session_principal=principal)  # type: ignore[arg-type]
    mcp.run(transport="stdio", show_banner=False)


def start_http(
    service: object,
    project_root: Path,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    *,
    auth: AuthProvider | None,
    suggestions: bool = False,
    token_ref: str | None = None,
    auth_mechanisms: list[str] | None = None,
) -> None:
    """Spawn a detached uvicorn HTTP daemon in the background and write the state file.

    Re-launches ``python -m canonic mcp start ... --_child`` via ``subprocess.Popen``
    (fork+exec) rather than calling ``os.fork()`` directly — see the module docstring
    for why a bare fork-without-exec is unsafe here. The relaunched process rebuilds
    its own ``CanonicService``/auth provider from ``project_root``/``token_ref``, since
    a fresh process cannot inherit live Python objects across ``exec()``.

    ``canonic mcp stop`` sends SIGTERM to the daemon via the recorded PID.

    ``auth`` is required (not optional): ``http`` transport is network-reachable once
    bound, so an unauthenticated daemon would be exactly the gap
    AMENDMENT-remote-mcp-transport.md closes. Callers must resolve an auth provider
    (``canonic.mcp.auth.build_mcp_auth``) before calling this function and raise their
    own user-facing error when none resolves — this function raises generically for
    any caller that skips that step. ``token_ref`` is passed through unchanged so the
    relaunched child can resolve the same provider itself. ``auth_mechanisms`` (e.g.
    ``["token", "oauth-proxy"]`` from ``canonic.mcp.auth.describe_auth_mechanisms``) is
    recorded in the state file for ``canonic mcp status`` to report.
    """
    if auth is None:
        raise RuntimeError(
            "http transport requires at least one auth mechanism — configure "
            "mcp.auth.tokens and/or mcp.auth.oauth in canonic.yaml, or pass --token-ref"
        )

    _check_version_on_start(project_root)

    existing = status(project_root)
    if existing.running:
        raise RuntimeError(
            f"MCP daemon is already running (PID {existing.pid}). Run `canonic mcp stop` first."
        )

    log_path = project_root / ".canonic" / "mcp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "canonic",
        "mcp",
        "start",
        "--transport",
        "http",
        "--project",
        str(project_root),
        "--host",
        host,
        "--port",
        str(port),
        "--_child",
    ]
    if token_ref is not None:
        cmd += ["--token-ref", token_ref]
    if suggestions:
        cmd.append("--suggestions")

    with open(log_path, "ab") as log_fh:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell, no user-controlled parts
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )

    _wait_for_daemon_ready(proc, host, port, log_path)

    state = DaemonState(
        pid=proc.pid,
        version=CANONIC_VERSION,
        transport="http",
        host=host,
        port=port,
        started_at=datetime.now(UTC).isoformat(),
        auth_enabled=True,
        auth_mechanisms=auth_mechanisms or [],
    )
    _write_state(project_root, state)


def serve_http_foreground(
    service: object,
    project_root: Path,
    host: str,
    port: int,
    *,
    auth: AuthProvider,
    suggestions: bool = False,
) -> None:
    """Run the uvicorn HTTP daemon in the current process (blocks until stopped).

    Only meant to be called from the detached child process spawned by ``start_http``
    (``canonic mcp start --transport http --_child``) — that process was created via
    ``exec()``, not ``os.fork()``, so it is safe here to touch DNS/TLS/logging from any
    thread. Do not call this directly from a long-lived multi-threaded process.
    """
    from canonic.config import load_config
    from canonic.log import _effective_log_params, configure_logging
    from canonic.mcp.server import build_server

    try:
        cfg = load_config(project_root / "canonic.yaml")
        level, file, format = _effective_log_params(
            cfg.logging.level, cfg.logging.file, cfg.logging.format
        )
    except Exception:
        level, file, format = _effective_log_params("WARNING", None)
    configure_logging(level=level, file=file, format=format)

    mcp = build_server(service, suggestions=suggestions, auth=auth)  # type: ignore[arg-type]
    import asyncio

    # stateless_http=True: no session IDs are issued or expected, so restarting the
    # daemon never leaves MCP clients stuck with a stale session ID that returns 404.
    asyncio.run(mcp.run_http_async(host=host, port=port, show_banner=False, stateless_http=True))


def _check_version_on_start(project_root: Path) -> None:
    """Warn loudly (raise ``RuntimeError``) when an existing daemon has a different version."""
    existing = status(project_root)
    if existing.running and existing.version_mismatch:
        raise RuntimeError(
            f"A Canonic MCP daemon is already running (PID {existing.pid}) "
            f"but its version ({existing.version!r}) differs from the current CLI "
            f"({existing.current_version!r}). "
            "Stop it first with `canonic mcp stop`, then start a new daemon."
        )
