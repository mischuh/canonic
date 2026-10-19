"""The ``decompose_change`` MCP tool: registration, payload parity and error mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from canonic.core.decompose_models import DecomposeRequest
from canonic.core.service import CanonicService
from canonic.mcp.server import build_server
from tests.conftest import MCP_ERAS, mcp_client
from tests.core.test_decompose import decompose_project  # noqa: F401 — fixture

if TYPE_CHECKING:
    from pathlib import Path

_REQUEST = {
    "metric": "result_rate",
    "period": {"dimension": "sale_month", "base": "2025-08", "compare": "2025-09"},
    "by": ["region"],
}


def _disable(project: Path) -> None:
    config = project / "canonic.yaml"
    config.write_text(config.read_text() + "decompose:\n  enabled: false\n")


async def _tool_names(service: CanonicService) -> set[str]:
    async with Client(build_server(service)) as client:
        return {tool.name for tool in await client.list_tools()}


class TestRegistration:
    async def test_registered_by_default(self, decompose_project: Path) -> None:  # noqa: F811
        assert "decompose_change" in await _tool_names(
            CanonicService.from_project(decompose_project)
        )

    async def test_absent_when_disabled_in_the_config(
        self,
        decompose_project: Path,  # noqa: F811
    ) -> None:
        _disable(decompose_project)
        assert "decompose_change" not in await _tool_names(
            CanonicService.from_project(decompose_project)
        )

    async def test_the_server_override_wins_over_the_config(
        self,
        decompose_project: Path,  # noqa: F811
    ) -> None:
        _disable(decompose_project)
        service = CanonicService.from_project(decompose_project, decompose=True)

        assert "decompose_change" in await _tool_names(service)
        async with Client(build_server(service)) as client:
            result = await client.call_tool("decompose_change", {"request": _REQUEST})
        assert result.data["method"] == "ratio_parts"

    async def test_no_decompose_hides_it(self, decompose_project: Path) -> None:  # noqa: F811
        service = CanonicService.from_project(decompose_project, decompose=False)
        assert "decompose_change" not in await _tool_names(service)


class TestCall:
    @pytest.mark.parametrize("era", MCP_ERAS)
    async def test_payload_matches_the_service(
        self,
        decompose_project: Path,  # noqa: F811
        era: str,
    ) -> None:
        service = CanonicService.from_project(decompose_project)
        async with mcp_client(build_server(service), era) as client:
            result = await client.call_tool("decompose_change", {"request": _REQUEST})

        direct = await service.decompose(DecomposeRequest.model_validate(_REQUEST))
        assert result.data == direct.model_dump(mode="json")

    async def test_a_malformed_request_is_validation_failed(
        self,
        decompose_project: Path,  # noqa: F811
    ) -> None:
        service = CanonicService.from_project(decompose_project)
        async with Client(build_server(service)) as client:
            result = await client.call_tool("decompose_change", {"request": {"metric": "revenue"}})
        assert result.data["code"] == "validation_failed"
        assert "period" in result.data["message"]

    async def test_an_open_period_is_a_structured_error(
        self,
        decompose_project: Path,  # noqa: F811
    ) -> None:
        service = CanonicService.from_project(decompose_project)
        request = {**_REQUEST, "as_of": "2025-09-20T00:00:00Z"}
        async with Client(build_server(service)) as client:
            result = await client.call_tool("decompose_change", {"request": request})
        assert result.data["code"] == "period_open"
