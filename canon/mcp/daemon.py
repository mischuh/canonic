"""MCP daemon lifecycle: start, stop, status and PID-file management (SPEC E8 §4.2).

State is written to ``.canon/mcp.json`` in the project root. Two transports:

- **stdio** (default) — the server runs in the foreground; the MCP client manages
  the process lifetime (``canon mcp start`` blocks until the client disconnects).
- **http** — a uvicorn-backed HTTP daemon is forked into the background; the PID
  file tracks the process so ``canon mcp stop/status`` work.

Version compatibility: the running Canon package version is stamped in the state
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

_STATE_FILE = ".canon/mcp.json"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7474


def _canon_version() -> str:
    try:
        return version("canon")
    except PackageNotFoundError:
        return "unknown"


@dataclass
class DaemonState:
    """Persisted daemon metadata (written to ``.canon/mcp.json``)."""

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
    """Runtime status as surfaced by ``canon mcp status``."""

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
    """Read and parse ``.canon/mcp.json``; returns ``None`` when absent."""
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

    current = _canon_version()
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


def start_stdio(service: object, project_root: Path) -> None:
    """Run the MCP server in stdio transport mode (foreground, blocks).

    The MCP client (e.g. Claude Code) manages the process lifetime; this function
    returns when the client disconnects or the process is killed.
    """
    from canon.mcp.server import build_server

    _check_version_on_start(project_root)
    mcp = build_server(service)  # type: ignore[arg-type]
    mcp.run(transport="stdio")


def start_http(
    service: object,
    project_root: Path,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
) -> None:
    """Fork a uvicorn HTTP daemon in the background and write the state file.

    The parent process returns immediately; the child runs ``mcp.run_http_async``.
    ``canon mcp stop`` sends SIGTERM to the child via the recorded PID.
    """
    _check_version_on_start(project_root)

    existing = status(project_root)
    if existing.running:
        raise RuntimeError(
            f"MCP daemon is already running (PID {existing.pid}). Run `canon mcp stop` first."
        )

    pid = os.fork()
    if pid != 0:
        # Parent: record state and return.
        state = DaemonState(
            pid=pid,
            version=_canon_version(),
            transport="http",
            host=host,
            port=port,
            started_at=datetime.now(UTC).isoformat(),
        )
        _write_state(project_root, state)
        return

    # Child: detach and run the server.
    # Redirect stdin/stdout/stderr to /dev/null at the FD level so that Python's
    # sys.std* objects stay open (logging handlers that reference them won't crash),
    # while the process is fully detached from the terminal.
    os.setsid()
    _null_r = os.open(os.devnull, os.O_RDONLY)
    _null_w = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null_r, 0)
    os.dup2(_null_w, 1)
    os.dup2(_null_w, 2)
    os.close(_null_r)
    os.close(_null_w)

    from canon.mcp.server import build_server

    mcp = build_server(service)  # type: ignore[arg-type]
    import asyncio

    asyncio.run(mcp.run_http_async(host=host, port=port, show_banner=False))
    sys.exit(0)


def _check_version_on_start(project_root: Path) -> None:
    """Warn loudly (raise ``RuntimeError``) when an existing daemon has a different version."""
    existing = status(project_root)
    if existing.running and existing.version_mismatch:
        raise RuntimeError(
            f"A Canon MCP daemon is already running (PID {existing.pid}) "
            f"but its version ({existing.version!r}) differs from the current CLI "
            f"({existing.current_version!r}). "
            "Stop it first with `canon mcp stop`, then start a new daemon."
        )
