"""Acceptance-criteria tests for the semantic-source loader (GH-5)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from canonic.exc import SemanticSourceError
from canonic.semantic.loader import (
    dump_semantic_source,
    list_semantic_sources,
    load_semantic_source,
)


def test_load_valid_source(write_source, valid_source_yaml: str) -> None:
    src = load_semantic_source(write_source(valid_source_yaml))
    assert src.name == "orders"
    assert len(src.measures) == 2


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SemanticSourceError, match="not found"):
        load_semantic_source(tmp_path / "nope.yaml")


def test_invalid_grain_reports_file_and_line(write_source) -> None:
    yaml = (
        "name: t\n"
        "connection: c\n"
        "table: s.t\n"
        "grain: [missing]\n"  # line 4
        "columns:\n"
        "  - { name: id, type: string }\n"
    )
    path = write_source(yaml, name="bad.yaml")
    with pytest.raises(SemanticSourceError) as exc:
        load_semantic_source(path)
    msg = str(exc.value)
    assert str(path) in msg
    assert re.search(r":\d+:", msg), f"expected a line number in {msg!r}"
    assert (
        msg.rstrip().endswith("4: grain column 'missing' is not a declared column") or "4:" in msg
    )


def test_measure_expr_undeclared_reports_error(write_source) -> None:
    yaml = (
        "name: t\nconnection: c\ntable: s.t\ngrain: [id]\n"
        "columns:\n  - { name: id, type: string }\n"
        "measures:\n  - { name: rev, expr: 'sum(nope)' }\n"
    )
    path = write_source(yaml, name="bad_measure.yaml")
    with pytest.raises(SemanticSourceError, match="undeclared column 'nope'"):
        load_semantic_source(path)


def test_bad_enum_reports_file_and_line(write_source) -> None:
    yaml = (
        "name: t\nconnection: c\ntable: s.t\ngrain: [id]\n"
        "columns:\n  - { name: id, type: nonsense }\n"  # invalid NormalizedType
    )
    path = write_source(yaml, name="bad_type.yaml")
    with pytest.raises(SemanticSourceError) as exc:
        load_semantic_source(path)
    assert str(path) in str(exc.value)


def test_list_returns_all_yaml_sorted(tmp_semantics_dir: Path) -> None:
    sources = list_semantic_sources(tmp_semantics_dir)
    assert [s.name for s in sources] == ["customers", "orders"]


def test_list_empty_when_no_semantics_dir(tmp_path: Path) -> None:
    assert list_semantic_sources(tmp_path) == []


def test_list_propagates_invalid_file(tmp_semantics_dir: Path) -> None:
    bad = tmp_semantics_dir / "semantics" / "warehouse_pg" / "broken.yaml"
    bad.write_text(
        "name: x\nconnection: c\ntable: s.t\ngrain: [ghost]\ncolumns:\n  - { name: id, type: string }\n"
    )
    with pytest.raises(SemanticSourceError, match="broken.yaml"):
        list_semantic_sources(tmp_semantics_dir)


def test_list_rejects_duplicate_name_across_connections(tmp_semantics_dir: Path) -> None:
    """A source name reused in a different connection's directory must be rejected.

    Joins and contract bindings reference sources by bare name with no connection
    qualifier, so a project-wide collision would otherwise silently shadow one source
    with another (last-loaded-wins) instead of erroring.
    """
    dupe = tmp_semantics_dir / "semantics" / "crm_mysql" / "orders.yaml"
    dupe.parent.mkdir(parents=True)
    dupe.write_text(
        "name: orders\nconnection: crm_mysql\ntable: legacy_orders\ngrain: [order_id]\n"
        "columns:\n  - { name: order_id, type: string, nullable: false }\n"
    )
    with pytest.raises(SemanticSourceError, match="duplicate source name 'orders'"):
        list_semantic_sources(tmp_semantics_dir)


def test_round_trip_load_dump_load(write_source, valid_source_yaml: str) -> None:
    original = load_semantic_source(write_source(valid_source_yaml))
    dumped = dump_semantic_source(original)
    reloaded = load_semantic_source(write_source(dumped, name="round.yaml"))
    assert original.model_dump() == reloaded.model_dump()
