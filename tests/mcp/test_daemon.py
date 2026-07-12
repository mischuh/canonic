"""Tests for daemon PID-file lifecycle (canonic/mcp/daemon.py)."""

from __future__ import annotations

import os
from pathlib import Path  # noqa: TC003

import pytest

from canonic.mcp.daemon import DaemonState, _canonic_version, read_state, start_http, status, stop


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".canonic").mkdir()
    return tmp_path


def _write_state_file(
    root: Path, pid: int, v: str, transport: str = "stdio", auth_enabled: bool = False
) -> None:
    state = DaemonState(
        pid=pid,
        version=v,
        transport=transport,
        host=None,
        port=None,
        started_at="2026-01-01T00:00:00+00:00",
        auth_enabled=auth_enabled,
    )
    (root / ".canonic" / "mcp.json").write_text(state.to_json())


class TestReadState:
    def test_absent(self, project_root: Path) -> None:
        assert read_state(project_root) is None

    def test_present(self, project_root: Path) -> None:
        _write_state_file(project_root, pid=12345, v="1.0.0")
        state = read_state(project_root)
        assert state is not None
        assert state.pid == 12345
        assert state.version == "1.0.0"

    def test_malformed_returns_none(self, project_root: Path) -> None:
        (project_root / ".canonic" / "mcp.json").write_text("not json{{{")
        assert read_state(project_root) is None


class TestStatus:
    def test_no_state_file(self, project_root: Path) -> None:
        s = status(project_root)
        assert not s.running
        assert s.pid is None

    def test_live_pid(self, project_root: Path) -> None:
        _write_state_file(project_root, pid=os.getpid(), v=_canonic_version())
        s = status(project_root)
        assert s.running
        assert s.pid == os.getpid()
        assert not s.version_mismatch

    def test_stale_pid_cleans_up(self, project_root: Path) -> None:
        # PID 1 is always alive but we use a fictional very-large PID that won't exist
        dead_pid = 99999999
        _write_state_file(project_root, pid=dead_pid, v="1.0.0")
        s = status(project_root)
        assert not s.running
        assert not (project_root / ".canonic" / "mcp.json").exists()

    def test_version_mismatch_detected(self, project_root: Path) -> None:
        _write_state_file(project_root, pid=os.getpid(), v="0.0.0-old")
        s = status(project_root)
        assert s.running
        assert s.version_mismatch
        assert s.version == "0.0.0-old"

    def test_auth_enabled_propagated(self, project_root: Path) -> None:
        _write_state_file(
            project_root, pid=os.getpid(), v=_canonic_version(), transport="http", auth_enabled=True
        )
        s = status(project_root)
        assert s.running
        assert s.auth_enabled is True


class TestStop:
    def test_no_daemon(self, project_root: Path) -> None:
        assert stop(project_root) is False

    def test_stale_pid(self, project_root: Path) -> None:
        _write_state_file(project_root, pid=99999999, v="1.0.0")
        result = stop(project_root)
        assert result is False
        assert not (project_root / ".canonic" / "mcp.json").exists()

    def test_live_pid_sends_sigterm(self, project_root: Path) -> None:
        import signal
        import subprocess
        import sys

        # Spawn a background sleep process we can safely kill.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            _write_state_file(project_root, pid=proc.pid, v=_canonic_version())
            result = stop(project_root)
            assert result is True
            assert not (project_root / ".canonic" / "mcp.json").exists()
            proc.wait(timeout=3)
            assert proc.returncode in (-signal.SIGTERM, -15, 1)
        finally:
            if proc.poll() is None:
                proc.kill()


class TestStartHttpAuth:
    """``start_http`` must fail closed when no auth verifier is supplied

    (AMENDMENT-remote-mcp-transport.md — http transport is network-reachable, so an
    unauthenticated daemon is exactly the gap the amendment closes).
    """

    def test_raises_without_auth(self, project_root: Path) -> None:
        with pytest.raises(RuntimeError, match="bearer token"):
            start_http(object(), project_root, auth=None)
        # Fails before ever forking/writing state.
        assert not (project_root / ".canonic" / "mcp.json").exists()
