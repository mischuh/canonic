"""Unit tests for live-definition rendering (SPEC-E6 §7, S6 AC1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.knowledge.rendering import DefinitionRenderer
from canonic.knowledge.validation import EntityIndex
from canonic.semantic.models import Column, Measure, NormalizedType, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage

_DIRECTIVE = "{{ sl:warehouse_pg.orders.total_revenue.expr }}"


def _index_with_revenue_expr(expr: str) -> EntityIndex:
    orders = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type=NormalizedType.STRING, nullable=False),
            Column(name="amount", type=NormalizedType.DECIMAL, nullable=False),
            Column(name="fx_rate", type=NormalizedType.DECIMAL, nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr=expr)],
    )
    return EntityIndex.from_sources([orders])


def test_directive_renders_live_expr(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A ``{{ sl:….expr }}`` directive renders the measure's live expr (S6 AC1)."""
    page = make_page(body=f"Revenue is {_DIRECTIVE} across orders.")

    rendered = DefinitionRenderer(entity_index).render(page)

    assert rendered == "Revenue is sum(amount) across orders."


def test_changed_expr_re_renders_without_editing_page(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """The same page reflects a changed measure expr automatically (S6 AC1)."""
    page = make_page(body=f"Revenue is {_DIRECTIVE}.")

    before = DefinitionRenderer(_index_with_revenue_expr("sum(amount)")).render(page)
    after = DefinitionRenderer(_index_with_revenue_expr("sum(amount * fx_rate)")).render(page)

    assert before == "Revenue is sum(amount)."
    assert after == "Revenue is sum(amount * fx_rate)."


def test_unresolved_directive_left_verbatim(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """An unknown entity is left untouched and never raises (drift-tolerant read side)."""
    body = "Mystery: {{ sl:warehouse_pg.orders.gone.expr }}."
    page = make_page(body=body)

    assert DefinitionRenderer(entity_index).render(page) == body


def test_non_expr_directive_ignored(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """Only ``.expr`` is supported in v1; other attribute forms are left verbatim."""
    body = "Name: {{ sl:warehouse_pg.orders.total_revenue.name }}."
    page = make_page(body=body)

    assert DefinitionRenderer(entity_index).render(page) == body
