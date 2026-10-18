"""A service built from a project follows its files instead of serving the state it started with."""

from __future__ import annotations

import os
from pathlib import Path  # noqa: TC003

import pytest

from canonic.compiler import SemanticQuery
from canonic.config import ProjectReload
from canonic.core import project as project_module
from canonic.core.project import ProjectState
from canonic.core.service import CanonicService
from canonic.exc import Unreachable, Unresolved

_ORDERS = "semantics/warehouse_pg/orders.yaml"
_REVENUE = "contracts/metrics/revenue.yaml"

_ORDER_COUNT = """\
metric: order_count
canonical:
  source: orders
  measure: order_count
status: active
"""


def _edit(root: Path, relative: str, text: str) -> None:
    """Rewrite a file and move its mtime forward, so a coarse clock cannot hide the change."""
    path = root / relative
    stat = path.stat() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if stat is not None:
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _with_order_count(root: Path) -> None:
    orders = (root / _ORDERS).read_text()
    _edit(
        root,
        _ORDERS,
        orders.replace(
            "measures:\n",
            "measures:\n  - name: order_count\n    expr: count(order_id)\n    additivity: additive\n",
        ),
    )


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The project roots parsed so far, to tell a reload from a signature check."""
    roots: list[Path] = []
    original = ProjectState.load.__func__  # type: ignore[attr-defined]

    def counting(cls: type[ProjectState], root: Path) -> ProjectState:
        roots.append(root)
        return original(cls, root)

    monkeypatch.setattr(project_module.ProjectState, "load", classmethod(counting))
    return roots


def test_a_new_contract_is_served_without_a_restart(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    assert "order_count" not in {m.metric for m in service.list_metrics()}

    _with_order_count(project_dir)
    _edit(project_dir, "contracts/metrics/order_count.yaml", _ORDER_COUNT)

    assert "order_count" in {m.metric for m in service.list_metrics()}
    assert service.compile_query(SemanticQuery(metrics=["order_count"])).sql


def test_a_changed_semantic_source_is_compiled_against(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    query = SemanticQuery(metrics=["revenue"], dimensions=["channel"])
    with pytest.raises(Unreachable):
        service.compile_query(query)

    orders = (project_dir / _ORDERS).read_text()
    columns = orders.replace(
        "columns:\n", "columns:\n  - { name: channel, type: string, nullable: false }\n"
    )
    _edit(
        project_dir,
        _ORDERS,
        columns.replace("dimensions:\n", "dimensions:\n  - { name: channel, column: channel }\n"),
    )

    assert "channel" in service.compile_query(query).sql


def test_a_removed_contract_is_no_longer_served(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    assert service.resolve_metric("revenue")

    (project_dir / _REVENUE).unlink()

    with pytest.raises(Unresolved):
        service.resolve_metric("revenue")


def test_unchanged_files_are_not_parsed_again(project_dir: Path, loads: list[Path]) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    loads.clear()

    for _ in range(3):
        service.list_metrics()

    assert loads == []


def test_an_invalid_edit_keeps_the_previous_state_and_names_the_file(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    _edit(project_dir, _REVENUE, "metric: revenue\ncanonical: [not, a, mapping\n")

    assert [m.metric for m in service.list_metrics()] == ["revenue"]
    warnings = service.compile_query(SemanticQuery(metrics=["revenue"])).warnings
    assert any("revenue.yaml" in w and "previous state" in w for w in warnings)


def test_the_same_broken_files_are_parsed_once(project_dir: Path, loads: list[Path]) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    loads.clear()
    _edit(project_dir, _REVENUE, "metric: revenue\ncanonical: [not, a, mapping\n")

    for _ in range(3):
        service.compile_query(SemanticQuery(metrics=["revenue"]))

    assert len(loads) == 1


def test_fixing_the_files_serves_the_new_state_and_drops_the_warning(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    good = (project_dir / _REVENUE).read_text()
    _edit(project_dir, _REVENUE, "metric: revenue\ncanonical: [not, a, mapping\n")
    service.list_metrics()

    _edit(
        project_dir, _REVENUE, good.replace('aliases: ["rev", "net revenue"]', "aliases: [sales]")
    )

    assert service.resolve_metric("sales").metric == "revenue"
    assert service.compile_query(SemanticQuery(metrics=["revenue"])).warnings == []


def test_a_request_finishes_on_the_state_it_started_with(project_dir: Path) -> None:
    service = CanonicService.from_project(project_dir, reload=ProjectReload.AUTO)
    ctx = service._ctx  # noqa: SLF001

    with ctx.request():
        _with_order_count(project_dir)
        _edit(project_dir, "contracts/metrics/order_count.yaml", _ORDER_COUNT)
        # A nested call, as a report running its sections makes, stays on the pinned state.
        assert "order_count" not in {m.metric for m in service.list_metrics()}

    assert "order_count" in {m.metric for m in service.list_metrics()}


def test_a_service_built_directly_serves_what_it_was_given(
    project_dir: Path, canonic_service: CanonicService
) -> None:
    before = {m.metric for m in canonic_service.list_metrics()}

    _with_order_count(project_dir)
    _edit(project_dir, "contracts/metrics/order_count.yaml", _ORDER_COUNT)

    assert {m.metric for m in canonic_service.list_metrics()} == before
