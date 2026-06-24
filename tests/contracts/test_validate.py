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

CUSTOMER_METRICS_SEMANTIC_YAML = """\
name: customer_metrics
connection: warehouse_pg
table: analytics.customer_health_scores
grain: [customer_id, month]
columns:
  - { name: customer_id, type: string, nullable: false }
  - { name: month, type: date, nullable: false }
  - { name: health_score, type: decimal, nullable: false }
measures:
  - name: health_score
    expr: health_score
    additivity: non_additive
dimensions:
  - { name: customer_id, column: customer_id }
  - { name: month, column: month }
"""

OPAQUE_BINDING_YAML = """\
metric: customer_health_score
canonical:
  kind: opaque
  source: customer_metrics
  measure: health_score
  native_grain: [customer_id, month]
status: active
"""


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


def _write_assertion(root: Path, name: str, content: str) -> None:
    d = root / "contracts" / "assertions"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content)


class TestValidateAssertions:
    """SPEC-Fuller-E15 §5.2: assertion query resolves; expect shape matches output."""

    def test_unresolved_metric_raises(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        _write_assertion(
            tmp_path,
            "ghost.yaml",
            "id: ghost-q1\nquery:\n  metrics: [ghost_metric]\nexpect:\n  rows: 1\n",
        )
        with pytest.raises(ContractError, match="ghost_metric"):
            validate_contracts(tmp_path)

    def test_expected_value_not_an_output_column_raises(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        _write_assertion(
            tmp_path,
            "bad.yaml",
            "id: bad-q1\nquery:\n  metrics: [revenue]\nexpect:\n  values:\n    profit: 1.0\n",
        )
        with pytest.raises(ContractError, match="profit"):
            validate_contracts(tmp_path)

    def test_scalar_query_expecting_many_rows_raises(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        _write_assertion(
            tmp_path,
            "rows.yaml",
            "id: rows-q1\nquery:\n  metrics: [revenue]\nexpect:\n  rows: 5\n",
        )
        with pytest.raises(ContractError, match="returns one row"):
            validate_contracts(tmp_path)

    def test_alias_metric_resolves(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        # "rev" is a declared alias of revenue in VALID_BINDING_YAML.
        _write_assertion(
            tmp_path,
            "alias.yaml",
            "id: alias-q1\nquery:\n  metrics: [rev]\nexpect:\n  values:\n    rev: 1.0\n",
        )
        validate_contracts(tmp_path)  # must not raise

    def test_non_executable_candidate_skipped(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        _write_assertion(
            tmp_path,
            "candidate.yaml",
            'id: usage-x\nquery:\n  native: "sum(amount)"\n  references: [orders.amount]\n'
            "expect: {}\n",
        )
        validate_contracts(tmp_path)  # raw candidate form is not validated yet


class TestValidateOpaque:
    """Validation tests for kind=opaque bindings (SPEC-Fuller-E15 §4.4, §8, S7)."""

    def test_valid_opaque_passes(self, tmp_path: Path) -> None:
        """A well-formed opaque binding with correct source, measure, and native_grain passes."""
        _write_binding(tmp_path, "score.yaml", OPAQUE_BINDING_YAML)
        _write_semantic(tmp_path, CUSTOMER_METRICS_SEMANTIC_YAML, name="customer_metrics.yaml")
        validate_contracts(tmp_path)  # must not raise

    def test_opaque_unknown_source_raises(self, tmp_path: Path) -> None:
        bad = OPAQUE_BINDING_YAML.replace("source: customer_metrics", "source: missing_source")
        _write_binding(tmp_path, "score.yaml", bad)
        _write_semantic(tmp_path, CUSTOMER_METRICS_SEMANTIC_YAML, name="customer_metrics.yaml")
        with pytest.raises(ContractError, match="missing_source"):
            validate_contracts(tmp_path)

    def test_opaque_unknown_measure_raises(self, tmp_path: Path) -> None:
        bad = OPAQUE_BINDING_YAML.replace("measure: health_score", "measure: no_such_measure")
        _write_binding(tmp_path, "score.yaml", bad)
        _write_semantic(tmp_path, CUSTOMER_METRICS_SEMANTIC_YAML, name="customer_metrics.yaml")
        with pytest.raises(ContractError, match="no_such_measure"):
            validate_contracts(tmp_path)

    def test_opaque_unknown_native_grain_column_raises(self, tmp_path: Path) -> None:
        """A native_grain entry that doesn't exist on the source → ContractError."""
        bad = OPAQUE_BINDING_YAML.replace(
            "native_grain: [customer_id, month]",
            "native_grain: [customer_id, phantom_col]",
        )
        _write_binding(tmp_path, "score.yaml", bad)
        _write_semantic(tmp_path, CUSTOMER_METRICS_SEMANTIC_YAML, name="customer_metrics.yaml")
        with pytest.raises(ContractError, match="phantom_col"):
            validate_contracts(tmp_path)

    def test_opaque_as_ratio_component_raises(self, tmp_path: Path) -> None:
        """An opaque metric used as a ratio component → ContractError (§4.4, S7 AC1)."""
        ratio_binding = """\
metric: ratio_with_opaque
canonical:
  kind: ratio
  numerator: customer_health_score
  denominator: revenue
status: active
"""
        _write_binding(tmp_path, "score.yaml", OPAQUE_BINDING_YAML)
        _write_binding(tmp_path, "revenue.yaml", VALID_BINDING_YAML)
        _write_binding(tmp_path, "ratio.yaml", ratio_binding)
        _write_semantic(tmp_path, CUSTOMER_METRICS_SEMANTIC_YAML, name="customer_metrics.yaml")
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML, name="orders.yaml")
        with pytest.raises(ContractError, match="opaque"):
            validate_contracts(tmp_path)


# Semantic source for a second table (denominator in cross-source ratio tests).
PAYMENTS_SEMANTIC_YAML = """\
name: payments
connection: warehouse_pg
table: analytics.fct_payments
grain: [payment_id]
columns:
  - { name: payment_id, type: string, nullable: false }
  - { name: amount,     type: decimal, nullable: false }
measures:
  - name: payment_count
    expr: count(payment_id)
    additivity: additive
"""


class TestValidatePopulationFilter:
    """Validation tests for population_filter column resolution (§4.5, S7 AC3)."""

    def test_single_leaf_filter_valid_column_passes(self, tmp_path: Path) -> None:
        """A population_filter referencing a declared column on the single leaf source passes."""
        binding = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
  population_filter: "status = 'completed'"
status: active
"""
        _write_binding(tmp_path, "revenue.yaml", binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        validate_contracts(tmp_path)  # must not raise

    def test_single_leaf_filter_phantom_column_raises(self, tmp_path: Path) -> None:
        """A population_filter referencing a column absent from the source → ContractError."""
        binding = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
  population_filter: "phantom_col = 'x'"
status: active
"""
        _write_binding(tmp_path, "revenue.yaml", binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        with pytest.raises(ContractError, match="phantom_col"):
            validate_contracts(tmp_path)

    def test_ratio_filter_absent_from_denominator_raises(self, tmp_path: Path) -> None:
        """AC3: ratio with population_filter column absent from the denominator's source → ContractError."""
        num_binding = """\
metric: order_count
canonical:
  source: orders
  measure: total_revenue
status: active
"""
        den_binding = """\
metric: payment_count
canonical:
  source: payments
  measure: payment_count
status: active
"""
        ratio_binding = """\
metric: revenue_per_payment
canonical:
  kind: ratio
  numerator: order_count
  denominator: payment_count
  population_filter: "status = 'completed'"
status: active
"""
        _write_binding(tmp_path, "num.yaml", num_binding)
        _write_binding(tmp_path, "den.yaml", den_binding)
        _write_binding(tmp_path, "ratio.yaml", ratio_binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML, name="orders.yaml")
        _write_semantic(tmp_path, PAYMENTS_SEMANTIC_YAML, name="payments.yaml")
        # "status" exists on orders (numerator leaf) but NOT on payments (denominator leaf)
        with pytest.raises(ContractError, match="status"):
            validate_contracts(tmp_path)

    def test_ratio_filter_valid_on_all_leaves_passes(self, tmp_path: Path) -> None:
        """A ratio population_filter whose column exists on all leaf sources passes."""
        num_binding = """\
metric: order_count
canonical:
  source: orders
  measure: total_revenue
status: active
"""
        den_binding = """\
metric: order_count_b
canonical:
  source: orders
  measure: total_revenue
status: active
"""
        ratio_binding = """\
metric: ratio_same_source
canonical:
  kind: ratio
  numerator: order_count
  denominator: order_count_b
  population_filter: "status = 'completed'"
status: active
"""
        _write_binding(tmp_path, "num.yaml", num_binding)
        _write_binding(tmp_path, "den.yaml", den_binding)
        _write_binding(tmp_path, "ratio.yaml", ratio_binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML, name="orders.yaml")
        validate_contracts(tmp_path)  # must not raise

    def test_malformed_population_filter_raises(self, tmp_path: Path) -> None:
        """A syntactically invalid population_filter → ContractError naming the metric."""
        binding = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
  population_filter: "status IN ("
status: active
"""
        _write_binding(tmp_path, "revenue.yaml", binding)
        _write_semantic(tmp_path, ORDERS_SEMANTIC_YAML)
        with pytest.raises(ContractError, match="revenue"):
            validate_contracts(tmp_path)
