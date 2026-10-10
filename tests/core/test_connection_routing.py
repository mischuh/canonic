"""Routing a compiled query to the connection that owns its first metric's source."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from canonic.compiler.result import CompileResult
from canonic.contracts.resolver import ContractResolver
from canonic.core.context import ServiceContext
from canonic.semantic.models import Column, SemanticSource

if TYPE_CHECKING:
    from pathlib import Path


def _context(tmp_path: Path) -> ServiceContext:
    orders = SemanticSource(
        name="orders",
        connection="warehouse_b",
        table="orders",
        grain=["order_id"],
        columns=[Column(name="order_id", type="int")],
    )
    return ServiceContext(
        config=MagicMock(),
        resolver=ContractResolver(bindings=(), guardrails=(), finality=(), assertions=()),
        sources=[orders],
        connection_dialects={},
        project_root=tmp_path,
        event_log=MagicMock(),
    )


@pytest.mark.parametrize(
    "resolved",
    ["orders.revenue", "cumulative(orders.revenue)", "opaque(orders.revenue)"],
)
def test_resolved_key_routes_to_its_source_connection(tmp_path: Path, resolved: str) -> None:
    compiled = CompileResult(sql="SELECT 1", dialect="postgres", resolved={"m": resolved})
    assert _context(tmp_path).connection_for_sql(compiled) == "warehouse_b"


def test_unknown_source_falls_back_to_the_default(tmp_path: Path) -> None:
    compiled = CompileResult(sql="SELECT 1", dialect="postgres", resolved={"m": "ratio(a, b)"})
    assert _context(tmp_path).connection_for_sql(compiled) is None
