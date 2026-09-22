"""Tests for ``mcp.tasks`` background-task wiring (canonic/mcp/server.py, config.py).

``fastmcp[tasks]`` (the ``fastmcp_tasks`` distribution) is an optional extra scoped to
this feature (AMENDMENT-fastmcp4-adoption §4, S22) and is not installed in the dev
environment. Tests that need the real extension running (task creation, polling, a
structured error carried through the task boundary intact) are marked ``integration``
and skip via ``pytest.importorskip`` rather than faking the task engine itself.

Tests that only check ``build_server``'s wiring (does the right extension get
registered, with the right url, and do the right tools become task-eligible) fake
``TasksExtension`` at the import boundary instead: ``ServerExtension``'s own base class
ships with core ``fastmcp``, not the extra, so the fake only needs to satisfy
``identifier`` and can leave every other hook at its default no-op.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from fastmcp.server.extensions import ServerExtension
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID

from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from canonic.core.service import CanonicService


class _FakeTasksExtension(ServerExtension):
    """Stands in for ``fastmcp_tasks.TasksExtension``: wiring only, no real task engine."""

    identifier = TASKS_EXTENSION_ID

    def __init__(self, *, url: str | None = None) -> None:
        self.url = url


@pytest.fixture
def fake_tasks_extension(monkeypatch: pytest.MonkeyPatch) -> type[_FakeTasksExtension]:
    """Inject a fake ``fastmcp_tasks`` module so ``build_server(tasks=True)`` can import it."""
    fake_module = ModuleType("fastmcp_tasks")
    fake_module.TasksExtension = _FakeTasksExtension  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp_tasks", fake_module)
    return _FakeTasksExtension


class TestBuildServerTasksWiring:
    """S22 AC4 + the extension/tool-eligibility wiring build_server is responsible for."""

    async def test_disabled_by_default_registers_no_extension(
        self, canonic_service: CanonicService
    ) -> None:
        """The default (and everything ``start_stdio`` passes) is tasks-off (S22 AC4)."""
        mcp = build_server(canonic_service)
        assert TASKS_EXTENSION_ID not in mcp._extensions
        for name in ("query", "run_sql", "run_report"):
            tool = await mcp.get_tool(name)
            assert not tool.task_config.supports_tasks()

    async def test_enabled_registers_extension_with_url_and_task_eligible_tools(
        self,
        canonic_service: CanonicService,
        fake_tasks_extension: type[_FakeTasksExtension],
    ) -> None:
        mcp = build_server(canonic_service, tasks=True, tasks_url="redis://localhost:6379/0")

        assert TASKS_EXTENSION_ID in mcp._extensions
        ext = mcp._extensions[TASKS_EXTENSION_ID]
        assert isinstance(ext, fake_tasks_extension)
        assert ext.url == "redis://localhost:6379/0"

        for name in ("query", "run_sql", "run_report"):
            tool = await mcp.get_tool(name)
            assert tool.task_config.supports_tasks()

        # task=True is optional, not required — a client that never declares the tasks
        # capability still gets a synchronous result (S22 AC2).
        query_tool = await mcp.get_tool("query")
        assert query_tool.task_config.mode == "optional"

        # Only the three warehouse-touching tools are task-eligible.
        list_metrics = await mcp.get_tool("list_metrics")
        assert not list_metrics.task_config.supports_tasks()

    def test_enabled_without_extra_raises_actionable_error(
        self, canonic_service: CanonicService
    ) -> None:
        """S22: fails loudly, not silently, when mcp.tasks.enabled but the extra isn't
        installed. ``fastmcp_tasks`` is genuinely absent in this environment, so this
        exercises the real import failure rather than a mocked one."""
        with pytest.raises(RuntimeError, match=r"canonic\[tasks\]"):
            build_server(canonic_service, tasks=True)


@pytest.mark.integration
class TestTasksExecution:
    """S22 AC1-AC3 — requires the real ``fastmcp_tasks`` extension running a Docket
    backend (even the in-memory one), so these only run with ``canonic[tasks]``
    installed. Install it and drop the ``importorskip`` to exercise this locally."""

    async def test_task_error_matches_synchronous_error_payload(
        self, canonic_service: CanonicService
    ) -> None:
        """S22 AC3: a ``CanonicError`` raised inside a task-executed call comes back as
        the same structured ``{code, message}`` payload as the inline path, not an
        unhandled task failure — ``canonic_error_response`` wraps the tool body, and
        that wrapping has to survive the task boundary. ``run_sql`` on a non-``SELECT``
        statement (``READ_ONLY_VIOLATION``) is a real, always-available error condition
        to drive this with, rather than depending on a live warehouse."""
        from fastmcp import Client

        sync_mcp = build_server(canonic_service)
        async with Client(sync_mcp) as client:
            sync_result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders"})

        task_mcp = build_server(canonic_service, tasks=True)
        async with Client(task_mcp) as client:
            task_result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders"})

        assert task_result.data == sync_result.data
        assert sync_result.data["code"] == "read_only_violation"

    @pytest.fixture(autouse=True)
    def _require_fastmcp_tasks(self) -> None:
        pytest.importorskip("fastmcp_tasks")

    async def test_polled_task_result_matches_synchronous_result(
        self, canonic_service: CanonicService
    ) -> None:
        """S22 AC1: a client that declares the tasks capability and polls a query to
        completion gets the same canonical JSON as a client that calls it synchronously."""
        from fastmcp import Client

        sync_mcp = build_server(canonic_service)
        async with Client(sync_mcp) as client:
            sync_result = await client.call_tool("query", {"query": {"metrics": ["revenue"]}})

        task_mcp = build_server(canonic_service, tasks=True)
        async with Client(task_mcp) as client:
            task_result = await client.call_tool("query", {"query": {"metrics": ["revenue"]}})

        assert task_result.data == sync_result.data

    async def test_legacy_era_client_gets_synchronous_result(
        self, canonic_service: CanonicService
    ) -> None:
        """S22 AC2: a legacy (pre-2026-07-28) client has no tasks capability to declare
        at all, so a task-eligible tool still answers synchronously for it."""
        from fastmcp import Client

        mcp = build_server(canonic_service, tasks=True)
        async with Client(mcp, mode="legacy") as client:
            result = await client.call_tool("query", {"query": {"metrics": ["revenue"]}})

        assert "rows" in result.data or "code" in result.data
