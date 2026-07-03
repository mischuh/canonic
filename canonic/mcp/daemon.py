"""MCP daemon lifecycle: start, stop, status and PID-file management (SPEC E8 §4.2).

State is written to ``.canonic/mcp.json`` in the project root. Two transports:

- **stdio** (default) — the server runs in the foreground; the MCP client manages
  the process lifetime (``canonic mcp start`` blocks until the client disconnects).
- **http** — a uvicorn-backed HTTP daemon is forked into the background; the PID
  file tracks the process so ``canonic mcp stop/status`` work.

Version compatibility: the running Canonic package version is stamped in the state
file so a mismatch is surfaced immediately (SPEC §4.2 AC2).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations

__all__ = [
    "DaemonState",
    "DaemonStatus",
    "read_state",
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
    suggestions: bool = False,
) -> None:
    """Fork a uvicorn HTTP daemon in the background and write the state file.

    The parent process returns immediately; the child runs ``mcp.run_http_async``.
    ``canonic mcp stop`` sends SIGTERM to the child via the recorded PID.
    """
    _check_version_on_start(project_root)

    existing = status(project_root)
    if existing.running:
        raise RuntimeError(
            f"MCP daemon is already running (PID {existing.pid}). Run `canonic mcp stop` first."
        )

    log_path = project_root / ".canonic" / "mcp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    pid = os.fork()
    if pid != 0:
        # Parent: record state and return.  The child may still be starting up;
        # give it a moment and confirm it is still alive before reporting success.
        import time

        time.sleep(0.2)
        if not _pid_alive(pid):
            hint = f"check {log_path} for details"
            raise RuntimeError(f"MCP daemon exited immediately after fork — {hint}")

        state = DaemonState(
            pid=pid,
            version=_canonic_version(),
            transport="http",
            host=host,
            port=port,
            started_at=datetime.now(UTC).isoformat(),
        )
        _write_state(project_root, state)
        return

    # Child: detach from the terminal and run the server.
    # stdin → /dev/null; stdout+stderr → .canonic/mcp.log so crashes are diagnosable.
    os.setsid()
    _null_r = os.open(os.devnull, os.O_RDONLY)
    _log_w = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(_null_r, 0)
    os.dup2(_log_w, 1)
    os.dup2(_log_w, 2)
    os.close(_null_r)
    os.close(_log_w)

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

    mcp = build_server(service, suggestions=suggestions)  # type: ignore[arg-type]
    import asyncio

    # stateless_http=True: no session IDs are issued or expected, so restarting the
    # daemon never leaves MCP clients stuck with a stale session ID that returns 404.
    asyncio.run(mcp.run_http_async(host=host, port=port, show_banner=False, stateless_http=True))
    sys.exit(0)


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
