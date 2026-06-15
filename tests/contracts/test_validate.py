"""Tests for canon/contracts/validate.py — cross-surface validation, AC2."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canon.contracts.validate import validate_contracts
from canon.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

from tests.contracts.conftest import (
    ORDERS_SEMANTIC_YAML,
    VALID_BINDING_YAML,
)


def _write_binding(root: Path, name: str, content: str) -> None:
    d = root / "contracts" / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content)


def _write_guardrail(root: Path, name: str, content: str) -> None:
    d = root / "contracts" / "guardrails"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content)


def _write_semantic(root: Path, content: str, name: str = "orders.yaml") -> None:
    d = root / "semantics" / "warehouse_pg"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content)


class TestValidateContracts:
    def test_valid_set_passes(self, tmp_contracts_dir: Path) -> None:
        validate_contracts(tmp_contracts_dir)  # must not raise

    def test_ac2_guardrail_nonexistent_source(self, tmp_path: Path) -> None:
        """AC2: applies_to.source pointing at a non-existent source → ContractError."""
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        bad_guardrail = (
            "id: bad-guard\n"
            "applies_to:\n"
            "  source: nonexistent_table\n"
            "kind: mandatory_filter\n"
            'filter: "x = 1"\n'
            "severity: error\n"
            "rationale: bad\n"
        )
        _write_guardrail(tmp_path, "bad.yaml", bad_guardrail)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)

        with pytest.raises(ContractError, match="nonexistent_table"):
            validate_contracts(tmp_path)

    def test_binding_canonical_source_missing(self, tmp_path: Path) -> None:
        bad_binding = VALID_BINDING_YAML.replace("source: orders", "source: missing_source")
        _write_binding(tmp_path, "revenue.yaml", bad_binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)

        with pytest.raises(ContractError, match="missing_source"):
            validate_contracts(tmp_path)

    def test_binding_canonical_measure_missing(self, tmp_path: Path) -> None:
        bad_binding = VALID_BINDING_YAML.replace(
            "measure: total_revenue", "measure: no_such_measure"
        )
        _write_binding(tmp_path, "revenue.yaml", bad_binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)

        with pytest.raises(ContractError, match="no_such_measure"):
            validate_contracts(tmp_path)

    def test_guardrail_nonexistent_metric(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        metric_guardrail = (
            "id: bad-metric-guard\n"
            "applies_to:\n"
            "  metric: ghost_metric\n"
            "kind: mandatory_filter\n"
            'filter: "x = 1"\n'
            "severity: error\n"
            "rationale: no such metric\n"
        )
        _write_guardrail(tmp_path, "bad.yaml", metric_guardrail)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)

        with pytest.raises(ContractError, match="ghost_metric"):
            validate_contracts(tmp_path)

    def test_no_contracts_no_error(self, tmp_path: Path) -> None:
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        validate_contracts(tmp_path)  # must not raise when contracts dirs are absent

    def test_deprecated_binding_not_validated(self, tmp_path: Path) -> None:
        """Deprecated bindings with non-existent sources should not raise."""
        deprecated = VALID_BINDING_YAML.replace("source: orders", "source: missing_source").replace(
            "status: active", "status: deprecated"
        )
        _write_binding(tmp_path, "revenue.yaml", deprecated)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        validate_contracts(tmp_path)  # must not raise
