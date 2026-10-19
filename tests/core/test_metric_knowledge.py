"""``describe_metric`` and ``list_metrics`` name the knowledge pages that define a metric."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.core.service import CanonicService
from canonic.exc import Unresolved

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_STATUS = "warehouse_pg.orders.status"

_COMPLETED = """\
metric: completed_revenue
canonical:
  source: orders
  measure: total_revenue
  population_filter: "status = 'completed'"
status: active
"""

_SHARE = """\
metric: completed_share
canonical:
  kind: ratio
  numerator: completed_revenue
  denominator: revenue
status: active
"""


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _page(
    root: Path,
    page_id: str,
    *,
    usage_mode: str = "definition",
    sl_refs: tuple[str, ...] = (),
    metrics: tuple[str, ...] = (),
    scope: str = "global",
) -> None:
    front = f"usage_mode: {usage_mode}\nsl_refs: {list(sl_refs)}\nmetrics: {list(metrics)}\n"
    location = (
        f"knowledge/{scope}/{page_id}.md"
        if scope == "global"
        else f"knowledge/user/me/{page_id}.md"
    )
    _write(root, location, f"---\n{front}---\nBody of {page_id}.\n")


@pytest.fixture
def project(project_dir: Path) -> Path:
    _write(project_dir, "contracts/metrics/completed_revenue.yaml", _COMPLETED)
    _write(project_dir, "contracts/metrics/completed_share.yaml", _SHARE)
    return project_dir


@pytest.fixture
def refs(project: Path) -> Callable[[str], list[str]]:
    def _refs(metric: str) -> list[str]:
        return CanonicService.from_project(project).describe_metric(metric).knowledge_refs

    return _refs


def test_a_project_without_pages_has_no_refs(refs: Callable[[str], list[str]]) -> None:
    assert refs("completed_revenue") == []


def test_a_page_that_names_the_metric_is_returned(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "completed-revenue", metrics=("completed_revenue",))
    _page(project, "unrelated", metrics=("revenue",))

    assert refs("completed_revenue") == ["completed-revenue"]


def test_the_metrics_of_a_shared_measure_get_their_own_pages(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "completed-revenue", metrics=("completed_revenue",))
    _page(project, "revenue-def", metrics=("revenue",))

    assert refs("revenue") == ["revenue-def"]
    assert refs("completed_revenue") == ["completed-revenue"]


def test_a_definition_page_on_a_column_of_the_filter_is_returned(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "what-completed-means", sl_refs=(_STATUS,))

    assert refs("completed_revenue") == ["what-completed-means"]
    assert refs("revenue") == []


def test_a_page_on_the_dimension_declared_on_the_filter_column_is_returned(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "by-status", sl_refs=("warehouse_pg.orders.status",), usage_mode="caveat")

    assert refs("completed_revenue") == ["by-status"]


@pytest.mark.parametrize("usage_mode", ["reference", "policy"])
def test_reference_and_policy_pages_on_the_filter_column_are_not_returned(
    project: Path, refs: Callable[[str], list[str]], usage_mode: str
) -> None:
    _page(project, "p", sl_refs=(_STATUS,), usage_mode=usage_mode)

    assert refs("completed_revenue") == []


def test_a_page_on_the_measure_alone_is_not_returned(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "about-total-revenue", sl_refs=("warehouse_pg.orders.total_revenue",))

    assert refs("completed_revenue") == []
    assert refs("revenue") == []


def test_a_named_page_replaces_the_pages_found_by_entity(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "named", metrics=("completed_revenue",))
    _page(project, "overlap", sl_refs=(_STATUS,))

    assert refs("completed_revenue") == ["named"]


def test_declared_ids_replace_everything_and_drop_unknown_ones(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _write(
        project,
        "contracts/metrics/completed_revenue.yaml",
        _COMPLETED + "knowledge_refs: [declared, missing]\n",
    )
    _page(project, "declared")
    _page(project, "named", metrics=("completed_revenue",))
    _page(project, "overlap", sl_refs=(_STATUS,))

    assert refs("completed_revenue") == ["declared"]


def test_a_personal_page_is_never_returned(project: Path, refs: Callable[[str], list[str]]) -> None:
    _page(project, "mine", metrics=("completed_revenue",), scope="user")

    assert refs("completed_revenue") == []


def test_a_composite_gets_its_own_pages_and_not_those_of_its_components(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _write(
        project,
        "contracts/metrics/completed_revenue.yaml",
        _COMPLETED + "knowledge_refs: [component-page]\n",
    )
    _page(project, "component-page")
    _page(project, "share-def", metrics=("completed_share",))

    assert refs("completed_share") == ["share-def"]
    assert refs("completed_revenue") == ["component-page"]


def test_a_composite_without_a_page_of_its_own_uses_the_filters_of_its_components(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _page(project, "what-completed-means", sl_refs=(_STATUS,))

    assert refs("completed_share") == ["what-completed-means"]


def test_the_composites_own_filter_is_read_against_each_leaf(
    project: Path, refs: Callable[[str], list[str]]
) -> None:
    _write(
        project,
        "contracts/metrics/completed_share.yaml",
        _SHARE.replace("status: active", "status: active", 1).replace(
            "  denominator: revenue\n",
            "  denominator: revenue\n  population_filter: \"status != 'refunded'\"\n",
        ),
    )
    _page(project, "refund-note", sl_refs=(_STATUS,), usage_mode="caveat")

    assert refs("completed_share") == ["refund-note"]


def test_list_metrics_carries_the_same_refs(project: Path) -> None:
    _page(project, "completed-revenue", metrics=("completed_revenue",))
    service = CanonicService.from_project(project)

    listed = {m.metric: m.knowledge_refs for m in service.list_metrics()}

    assert listed["completed_revenue"] == ["completed-revenue"]
    assert listed["revenue"] == []
    assert listed["completed_share"] == []
    assert (
        listed["completed_revenue"] == service.describe_metric("completed_revenue").knowledge_refs
    )


def test_an_unknown_metric_is_still_unresolved(project: Path) -> None:
    with pytest.raises(Unresolved):
        CanonicService.from_project(project).describe_metric("nope")


def test_a_service_built_in_memory_without_a_project_has_no_refs(
    canonic_service: CanonicService,
) -> None:
    assert canonic_service.describe_metric("revenue").knowledge_refs == []
    assert all(m.knowledge_refs == [] for m in canonic_service.list_metrics())
