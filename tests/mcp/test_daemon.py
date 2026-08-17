"""Tests for daemon PID-file lifecycle (canonic/mcp/daemon.py)."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path  # noqa: TC003

import pytest

from canonic import __version__ as CANONIC_VERSION
from canonic.mcp.daemon import (
    DaemonState,
    _wait_for_daemon_ready,
    read_state,
    start_http,
    start_stdio,
    status,
    stop,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".canonic").mkdir()
    return tmp_path


def _write_state_file(
    root: Path,
    pid: int,
    v: str,
    transport: str = "stdio",
    auth_enabled: bool = False,
    auth_mechanisms: list[str] | None = None,
) -> None:
    state = DaemonState(
        pid=pid,
        version=v,
        transport=transport,
        host=None,
        port=None,
        started_at="2026-01-01T00:00:00+00:00",
        auth_enabled=auth_enabled,
        auth_mechanisms=auth_mechanisms or [],
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
        _write_state_file(project_root, pid=os.getpid(), v=CANONIC_VERSION)
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
            project_root, pid=os.getpid(), v=CANONIC_VERSION, transport="http", auth_enabled=True
        )
        s = status(project_root)
        assert s.running
        assert s.auth_enabled is True

    def test_auth_mechanisms_propagated(self, project_root: Path) -> None:
        _write_state_file(
            project_root,
            pid=os.getpid(),
            v=CANONIC_VERSION,
            transport="http",
            auth_enabled=True,
            auth_mechanisms=["token", "oauth-proxy"],
        )
        s = status(project_root)
        assert s.running
        assert s.auth_mechanisms == ["token", "oauth-proxy"]

    def test_legacy_state_file_without_auth_mechanisms_still_parses(
        self, project_root: Path
    ) -> None:
        # A state file written before auth_mechanisms existed has no such key.
        legacy_state = {
            "pid": os.getpid(),
            "version": CANONIC_VERSION,
            "transport": "http",
            "host": None,
            "port": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "auth_enabled": True,
        }
        (project_root / ".canonic" / "mcp.json").write_text(json.dumps(legacy_state))
        s = status(project_root)
        assert s.running
        assert s.auth_enabled is True
        assert s.auth_mechanisms == []


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
            _write_state_file(project_root, pid=proc.pid, v=CANONIC_VERSION)
            result = stop(project_root)
            assert result is True
            assert not (project_root / ".canonic" / "mcp.json").exists()
            proc.wait(timeout=3)
            assert proc.returncode in (-signal.SIGTERM, -15, 1)
        finally:
            if proc.poll() is None:
                proc.kill()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestWaitForDaemonReady:
    """The readiness check ``start_http`` runs before declaring success (SPEC E8 §4.2).

    Regression coverage for the bug where a fixed 0.2s sleep + bare ``proc.poll()``
    reported "started" for a child that hadn't crashed *yet* but was either still
    initializing or about to fail to bind (e.g. a stale daemon already holding the
    port) — ``canonic mcp status`` would then report "not running" moments later.
    """

    def test_returns_once_port_is_listening(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            _wait_for_daemon_ready(proc, "127.0.0.1", port, tmp_path / "mcp.log", timeout=2.0)
        finally:
            server.close()
            proc.kill()
            proc.wait(timeout=3)

    def test_bind_all_host_checks_loopback(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            # host="0.0.0.0" isn't itself a connectable address; the check must
            # probe loopback instead of failing/timing out.
            _wait_for_daemon_ready(proc, "0.0.0.0", port, tmp_path / "mcp.log", timeout=2.0)
        finally:
            server.close()
            proc.kill()
            proc.wait(timeout=3)

    def test_raises_when_process_exits_before_ready(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=3)  # ensure it has actually exited before we check
        log_path = tmp_path / "mcp.log"
        with pytest.raises(RuntimeError, match="exited before becoming ready"):
            _wait_for_daemon_ready(proc, "127.0.0.1", _free_port(), log_path, timeout=2.0)

    def test_timeout_kills_process_and_raises(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        # Nothing ever listens on this port, so readiness can never be confirmed.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with pytest.raises(RuntimeError, match="did not start accepting connections"):
                _wait_for_daemon_ready(
                    proc, "127.0.0.1", _free_port(), tmp_path / "mcp.log", timeout=0.3
                )
            proc.wait(timeout=3)
            assert proc.poll() is not None, "timed-out process should have been killed"
        finally:
            if proc.poll() is None:
                proc.kill()


class TestStartHttpAuth:
    """``start_http`` must fail closed when no auth provider is supplied

    (AMENDMENT-remote-mcp-transport.md, AMENDMENT-oauth-mcp-auth.md — http transport is
    network-reachable, so an unauthenticated daemon is exactly the gap these amendments
    close).
    """

    def test_raises_without_auth(self, project_root: Path) -> None:
        with pytest.raises(RuntimeError, match="auth mechanism"):
            start_http(object(), project_root, auth=None)
        # Fails before ever forking/writing state.
        assert not (project_root / ".canonic" / "mcp.json").exists()


class _StubResolver:
    def __init__(self, *, tenancy_enabled: bool) -> None:
        self.tenancy_enabled = tenancy_enabled


class _StubService:
    """A minimal stand-in exposing only what ``start_stdio``'s guard reads."""

    def __init__(self, *, tenancy_enabled: bool) -> None:
        self.resolver = _StubResolver(tenancy_enabled=tenancy_enabled)


class TestStartStdioTenancyGuard:
    """``stdio`` has no per-request auth, so a tenancy policy requires ``--tenant``
    to bind a principal for the whole session (SPEC-E12 §5, S13 AC3).
    """

    def test_refuses_without_tenant_when_tenancy_enabled(self, project_root: Path) -> None:
        service = _StubService(tenancy_enabled=True)
        with pytest.raises(RuntimeError, match="tenancy policy"):
            start_stdio(service, project_root, principal=None)

    def test_no_tenancy_policy_starts_without_tenant(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _StubService(tenancy_enabled=False)
        called = {}
        monkeypatch.setattr(
            "canonic.mcp.server.build_server", lambda *a, **k: called.setdefault("built", True)
        )
        # mcp.run would block; the object returned above has no .run, so calling it
        # would raise — confirm we get past the guard by checking build_server ran.
        with pytest.raises(AttributeError):
            start_stdio(service, project_root, principal=None)
        assert called.get("built") is True

    def test_tenant_given_satisfies_guard_when_tenancy_enabled(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from canonic.contracts.principal import Principal

        service = _StubService(tenancy_enabled=True)
        called = {}
        monkeypatch.setattr(
            "canonic.mcp.server.build_server", lambda *a, **k: called.setdefault("built", True)
        )
        with pytest.raises(AttributeError):
            start_stdio(service, project_root, principal=Principal(tenant="4711"))
        assert called.get("built") is True
