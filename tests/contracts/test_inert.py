"""Declarations the compiler does not act on are reported, not silently ignored (§C15)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.inert import inert_declaration_warnings

if TYPE_CHECKING:
    from pathlib import Path

_SOURCE = """\
name: {name}
connection: db
table: {name}
grain: [id]
columns:
  - {{name: id, type: string, nullable: false}}
  - {{name: val, type: decimal, nullable: true}}
measures:
  - {{name: total, expr: 'sum(val)', additivity: additive}}
{extra}"""

_FINALITY = """\
metric: revenue
realizations:
  - {{ source: orders, role: final, watermark: "business_day - 1 day", tz: "America/New_York" }}
  - {{ source: orders_rt, role: provisional }}
{extra}"""

_RESTRICT = """\
id: board-final-only
applies_to: { metric: revenue }
kind: restrict_source
restrict_to: { role: final }
context: board_reporting
rationale: "Boards see final numbers."
"""


def _write_source(root: Path, name: str, extra: str = "") -> None:
    path = root / "semantics" / "db" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SOURCE.format(name=name, extra=extra))


def _write_finality(root: Path, extra: str) -> None:
    for name in ("orders", "orders_rt"):
        _write_source(root, name)
    path = root / "contracts" / "guardrails" / "finality-revenue.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FINALITY.format(extra=extra))


def test_clean_project_has_no_warnings(tmp_path: Path) -> None:
    _write_source(tmp_path, "orders", "filters: []\nsegments: []\n")
    assert inert_declaration_warnings(tmp_path) == []


def test_named_filters_are_reported(tmp_path: Path) -> None:
    _write_source(tmp_path, "orders", 'filters:\n  - {name: completed, expr: "val > 0"}\n')
    (warning,) = inert_declaration_warnings(tmp_path)
    assert "orders" in warning
    assert "completed" in warning


def test_segments_are_reported(tmp_path: Path) -> None:
    _write_source(tmp_path, "orders", "segments:\n  - big_orders\n")
    (warning,) = inert_declaration_warnings(tmp_path)
    assert "segments" in warning


def test_canonical_coalescing_is_not_reported(tmp_path: Path) -> None:
    _write_finality(tmp_path, 'coalescing: "window  <=  watermark ? final : provisional"\n')
    assert inert_declaration_warnings(tmp_path) == []


def test_other_coalescing_is_reported(tmp_path: Path) -> None:
    _write_finality(tmp_path, 'coalescing: "always provisional"\n')
    (warning,) = inert_declaration_warnings(tmp_path)
    assert "coalescing" in warning
    assert "revenue" in warning


def test_board_only_final_without_restrict_source_is_reported(tmp_path: Path) -> None:
    _write_finality(tmp_path, "board_only_final: true\n")
    (warning,) = inert_declaration_warnings(tmp_path)
    assert "board_only_final" in warning
    assert "restrict_source" in warning


def test_board_only_final_with_restrict_source_is_not_reported(tmp_path: Path) -> None:
    _write_finality(tmp_path, "board_only_final: true\n")
    path = tmp_path / "contracts" / "guardrails" / "board-final-only.yaml"
    path.write_text(_RESTRICT)
    assert inert_declaration_warnings(tmp_path) == []
