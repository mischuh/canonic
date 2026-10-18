"""The fingerprint names the project files a service is serving, by content."""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from canonic.compiler import SemanticQuery
from canonic.config import ProjectReload
from canonic.connectors.base import Capability, ResultSet
from canonic.core.models import QueryMetadata
from canonic.core.project import project_fingerprint
from canonic.core.service import CanonicService

if TYPE_CHECKING:
    from pathlib import Path

_REVENUE = "contracts/metrics/revenue.yaml"


def _touch_forward(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)
    _touch_forward(path)


def test_the_same_project_checked_out_twice_has_the_same_fingerprint(
    project_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    copy = tmp_path_factory.mktemp("copy") / "project"
    shutil.copytree(project_dir, copy)
    for path in copy.rglob("*.yaml"):
        _touch_forward(path)

    assert project_fingerprint(copy) == project_fingerprint(project_dir)


def test_touching_a_file_without_changing_it_keeps_the_fingerprint(project_dir: Path) -> None:
    before = project_fingerprint(project_dir)

    _touch_forward(project_dir / _REVENUE)

    assert project_fingerprint(project_dir) == before


@pytest.mark.parametrize(
    "relative",
    ["canonic.yaml", _REVENUE, "semantics/warehouse_pg/orders.yaml"],
)
def test_a_content_change_in_any_watched_file_changes_the_fingerprint(
    project_dir: Path, relative: str
) -> None:
    before = project_fingerprint(project_dir)

    _append(project_dir / relative, "# edited\n")

    assert project_fingerprint(project_dir) != before


def test_a_renamed_file_changes_the_fingerprint(project_dir: Path) -> None:
    before = project_fingerprint(project_dir)

    (project_dir / _REVENUE).rename(project_dir / "contracts/metrics/sales.yaml")

    assert project_fingerprint(project_dir) != before


def test_files_outside_the_loaded_directories_do_not_count(project_dir: Path) -> None:
    before = project_fingerprint(project_dir)

    (project_dir / "notes.yaml").write_text("anything: 1\n")
    (project_dir / "knowledge").mkdir()
    (project_dir / "knowledge" / "page.md").write_text("# a page\n")

    assert project_fingerprint(project_dir) == before


def test_a_service_reports_the_fingerprint_it_loaded(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    info = service.project_info()

    assert info.fingerprint == project_fingerprint(project_dir)
    assert info.loaded_at is not None


def test_a_reload_moves_the_fingerprint_and_the_load_time(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    first = service.project_info()

    _append(project_dir / _REVENUE, "# edited\n")
    second = service.project_info()

    assert second.fingerprint != first.fingerprint
    assert second.fingerprint == project_fingerprint(project_dir)
    assert first.loaded_at is not None
    assert second.loaded_at is not None
    assert second.loaded_at > first.loaded_at


def test_the_fingerprint_stays_while_the_files_stay(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)
    first = service.project_info()

    assert service.project_info() == first


def test_a_failed_reload_keeps_reporting_the_state_being_served(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    served = service.project_info()

    (project_dir / _REVENUE).write_text("metric: revenue\ncanonical: [not, a, mapping\n")

    assert service.project_info() == served


def test_a_service_built_in_memory_has_no_fingerprint(canonic_service: CanonicService) -> None:
    info = canonic_service.project_info()

    assert info.fingerprint is None
    assert info.loaded_at is None


def test_compile_metadata_carries_the_fingerprint(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir)

    compiled = service.compile_query(SemanticQuery(metrics=["revenue"]))
    metadata = QueryMetadata.from_compile_result(compiled)

    assert metadata.project_fingerprint == project_fingerprint(project_dir)


def test_compile_metadata_of_an_in_memory_service_has_none(
    canonic_service: CanonicService,
) -> None:
    compiled = canonic_service.compile_query(SemanticQuery(metrics=["revenue"]))

    assert QueryMetadata.from_compile_result(compiled).project_fingerprint is None


async def test_the_answer_event_records_the_fingerprint(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = MagicMock()
    connector.capabilities.return_value = [Capability.RUN_READ_ONLY_SQL]
    connector.run_read_only_sql = AsyncMock(return_value=ResultSet(columns=[], rows=[[6]]))
    connector.aclose = AsyncMock()

    class _Factory:
        def for_id(self, _config: Any, _connection: Any) -> Any:
            return connector

    monkeypatch.setattr("canonic.core.context.default_factory", _Factory())
    service = CanonicService.from_project(project_dir)

    result = await service.query(SemanticQuery(metrics=["revenue"]))

    log = project_dir / ".canonic" / "events.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["project_fingerprint"] for e in events] == [project_fingerprint(project_dir)]
    assert result.metadata.project_fingerprint == project_fingerprint(project_dir)
