"""``canonic status`` prints the fingerprint a running server reports in ``contract_info``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastmcp import Client
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.core.project import project_fingerprint
from canonic.core.service import CanonicService
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from pathlib import Path


def test_status_json_carries_the_project_fingerprint(project_dir: Path) -> None:
    result = CliRunner().invoke(app, ["--json", "status"])

    assert result.exit_code == 0
    assert json.loads(result.output)["project_fingerprint"] == project_fingerprint(project_dir)


def test_status_text_prints_the_project_fingerprint(project_dir: Path) -> None:
    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert f"project:        {project_fingerprint(project_dir)}" in result.output


async def test_status_and_a_running_server_report_the_same_fingerprint(project_dir: Path) -> None:
    cli = json.loads(CliRunner().invoke(app, ["--json", "status"]).output)

    async with Client(build_server(CanonicService.from_project(project_dir))) as client:
        info = (await client.call_tool("contract_info", {})).data

    assert info["project_fingerprint"] == cli["project_fingerprint"]
    assert info["project_loaded_at"] is not None
