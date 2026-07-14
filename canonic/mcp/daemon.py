"""MCP daemon lifecycle: start, stop, status and PID-file management (SPEC E8 §4.2).

State is written to ``.canonic/mcp.json`` in the project root. Two transports:

- **stdio** (default) — the server runs in the foreground; the MCP client manages
  the process lifetime (``canonic mcp start`` blocks until the client disconnects).
- **http** — a uvicorn-backed HTTP daemon runs detached in the background; the PID
  file tracks the process so ``canonic mcp stop/status`` work. Network-reachable, so
  it requires a bearer-token verifier (AMENDMENT-remote-mcp-transport.md) — ``stdio``
  needs none.

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
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canonic.mcp.auth import CanonicTokenVerifier

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


def _canonic_version() -> str:
    try:
        return version("canonic")
    except PackageNotFoundError:
        return "unknown"


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

    current = _canonic_version()
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


def start_stdio(service: object, project_root: Path, *, suggestions: bool = False) -> None:
    """Run the MCP server in stdio transport mode (foreground, blocks).

    The MCP client (e.g. Claude Code) manages the process lifetime; this function
    returns when the client disconnects or the process is killed.
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
    mcp = build_server(service, suggestions=suggestions)  # type: ignore[arg-type]
    mcp.run(transport="stdio", show_banner=False)


def start_http(
    service: object,
    project_root: Path,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    *,
    auth: CanonicTokenVerifier | None,
    suggestions: bool = False,
    token_ref: str | None = None,
) -> None:
    """Spawn a detached uvicorn HTTP daemon in the background and write the state file.

    Re-launches ``python -m canonic mcp start ... --_child`` via ``subprocess.Popen``
    (fork+exec) rather than calling ``os.fork()`` directly — see the module docstring
    for why a bare fork-without-exec is unsafe here. The relaunched process rebuilds
    its own ``CanonicService``/auth verifier from ``project_root``/``token_ref``, since
    a fresh process cannot inherit live Python objects across ``exec()``.

    ``canonic mcp stop`` sends SIGTERM to the daemon via the recorded PID.

    ``auth`` is required (not optional): ``http`` transport is network-reachable once
    bound, so an unauthenticated daemon would be exactly the gap
    AMENDMENT-remote-mcp-transport.md closes. Callers must resolve a token verifier
    (``canonic.mcp.auth.build_token_verifier``) before calling this function and raise
    their own user-facing error when none resolves — this function raises generically
    for any caller that skips that step. ``token_ref`` is passed through unchanged so
    the relaunched child can resolve the same verifier itself.
    """
    if auth is None:
        raise RuntimeError(
            "http transport requires at least one bearer token — configure mcp.auth.tokens "
            "in canonic.yaml or pass --token-ref"
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

    # The child may still be starting up; give it a moment and confirm it is still
    # alive before reporting success.
    import time

    time.sleep(0.2)
    if proc.poll() is not None:
        hint = f"check {log_path} for details"
        raise RuntimeError(f"MCP daemon exited immediately after starting — {hint}")

    state = DaemonState(
        pid=proc.pid,
        version=_canonic_version(),
        transport="http",
        host=host,
        port=port,
        started_at=datetime.now(UTC).isoformat(),
        auth_enabled=True,
    )
    _write_state(project_root, state)


def serve_http_foreground(
    service: object,
    project_root: Path,
    host: str,
    port: int,
    *,
    auth: CanonicTokenVerifier,
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
