"""``project.reload`` decides whether a server follows its files, and off reports drift instead."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastmcp import Client

from canonic.compiler import SemanticQuery
from canonic.config import ProjectReload
from canonic.core import project as project_module
from canonic.core.service import CanonicService
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_REVENUE = "contracts/metrics/revenue.yaml"

_ORDER_COUNT = """\
metric: order_count
canonical:
  source: orders
  measure: order_count
status: active
"""


def _edit(root: Path, relative: str, text: str) -> None:
    path = root / relative
    stat = path.stat() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if stat is not None:
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _add_order_count(root: Path) -> None:
    _edit(root, "contracts/metrics/order_count.yaml", _ORDER_COUNT)


def _metrics(service: CanonicService) -> set[str]:
    return {m.metric for m in service.list_metrics()}


def test_a_project_without_the_setting_does_not_reload(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    _add_order_count(project_dir)

    assert "order_count" not in _metrics(service)
    assert service.project_info().reload is ProjectReload.OFF


def test_the_config_can_turn_reload_on(project_dir: Path) -> None:
    config = project_dir / "canonic.yaml"
    config.write_text(config.read_text().replace("project:\n", "project:\n  reload: auto\n"))
    service = CanonicService.from_project(project_dir)

    _add_order_count(project_dir)

    assert service.project_info().reload is ProjectReload.AUTO
    assert "order_count" in _metrics(service)


def test_the_argument_overrides_the_config(project_dir: Path) -> None:
    config = project_dir / "canonic.yaml"
    config.write_text(config.read_text().replace("project:\n", "project:\n  reload: auto\n"))
    service = CanonicService.from_project(project_dir, reload=ProjectReload.OFF)

    _add_order_count(project_dir)

    assert "order_count" not in _metrics(service)


def test_off_serves_the_state_loaded_at_startup_for_every_kind_of_edit(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)
    served = service.project_info()

    _add_order_count(project_dir)
    _edit(project_dir, _REVENUE, "metric: revenue\ncanonical: [not, a, mapping\n")
    (project_dir / "semantics/warehouse_pg/orders.yaml").unlink()

    assert _metrics(service) == {"revenue"}
    assert service.compile_query(SemanticQuery(metrics=["revenue"])).sql
    info = service.project_info()
    assert (info.fingerprint, info.loaded_at) == (served.fingerprint, served.loaded_at)


def test_off_reports_nothing_while_the_files_match(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    assert service.project_info().files_changed is False


def test_off_reports_a_content_change_without_applying_it(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    _add_order_count(project_dir)

    assert service.project_info().files_changed is True
    assert "order_count" not in _metrics(service)


def test_off_does_not_report_a_touch(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    _edit(project_dir, _REVENUE, (project_dir / _REVENUE).read_text())

    assert service.project_info().files_changed is False


def test_off_stops_reporting_once_the_change_is_reverted(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)
    original = (project_dir / _REVENUE).read_text()

    _edit(project_dir, _REVENUE, original + "# edited\n")
    assert service.project_info().files_changed is True
    _edit(project_dir, _REVENUE, original)

    assert service.project_info().files_changed is False


def test_off_hashes_the_files_once_per_change(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CanonicService.from_project(project_dir)
    hashed: list[Path] = []
    original = project_module.project_fingerprint

    def counting(root: Path) -> str:
        hashed.append(root)
        return original(root)

    monkeypatch.setattr(project_module, "project_fingerprint", counting)
    _add_order_count(project_dir)

    for _ in range(3):
        service.list_metrics()
        service.project_info()

    assert len(hashed) == 1


def test_off_adds_no_warning_to_a_response(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)
    _edit(project_dir, _REVENUE, "metric: revenue\ncanonical: [not, a, mapping\n")

    assert service.compile_query(SemanticQuery(metrics=["revenue"])).warnings == []


def test_a_service_built_in_memory_reports_no_drift(canonic_service: CanonicService) -> None:
    info = canonic_service.project_info()

    assert info.files_changed is False
    assert info.reload is ProjectReload.OFF


async def test_contract_info_reports_the_mode_and_the_drift(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    async with Client(build_server(service)) as client:
        before = (await client.call_tool("contract_info", {})).data
        _add_order_count(project_dir)
        after = (await client.call_tool("contract_info", {})).data

    assert (before["project_reload"], before["project_files_changed"]) == ("off", False)
    assert (after["project_reload"], after["project_files_changed"]) == ("off", True)
    assert after["project_fingerprint"] == before["project_fingerprint"]
