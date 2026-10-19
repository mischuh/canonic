"""The semantic entities a filter reads, as pages name them in ``sl_refs``."""

from __future__ import annotations

import pytest

from canonic.compiler import filter_entities
from canonic.semantic.models import Column, Dimension, SemanticSource

_ORDERS = SemanticSource(
    name="orders",
    connection="wh",
    table="orders",
    grain=["order_id"],
    columns=[
        Column(name="order_id", type="int", nullable=False),
        Column(name="status", type="string", nullable=False),
        Column(name="channel", type="string", nullable=False),
    ],
    dimensions=[
        Dimension(name="order_status", column="status"),
        Dimension(name="channel", column="channel"),
    ],
)
_SOURCES = {"orders": _ORDERS}


def test_a_column_gives_the_column_and_the_dimension_declared_on_it() -> None:
    assert filter_entities("status = 'completed'", "orders", _SOURCES) == (
        "wh.orders.status",
        "wh.orders.order_status",
    )


def test_several_columns_are_listed_once_each_in_order() -> None:
    assert filter_entities(
        "channel = 'web' AND status = 'x' OR channel = 'app'", "orders", _SOURCES
    ) == (
        "wh.orders.channel",
        "wh.orders.status",
        "wh.orders.order_status",
    )


def test_a_dimension_name_resolves_to_its_column() -> None:
    assert "wh.orders.status" in filter_entities("order_status = 'x'", "orders", _SOURCES)


@pytest.mark.parametrize("filter_sql", ["unknown_col = 1", "status ==== 'x'", ""])
def test_a_filter_that_does_not_resolve_reads_nothing(filter_sql: str) -> None:
    assert filter_entities(filter_sql, "orders", _SOURCES) == ()
