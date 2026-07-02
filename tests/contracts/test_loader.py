"""Tests for canonic/contracts/loader.py — YAML loading, file:line errors, AC1."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.contracts.loader import (
    contracts_dir_scaffold,
    load_assertions,
    load_guardrails,
    load_metric_bindings,
)
from canonic.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

from tests.contracts.conftest import (
    VALID_ASSERTION_YAML,
    VALID_BINDING_YAML,
    VALID_FINALITY_YAML,
    VALID_GUARDRAIL_YAML,
)


class TestLoadMetricBindings:
    def test_happy_path(self, tmp_contracts_dir: Path) -> None:
        bindings = load_metric_bindings(tmp_contracts_dir)
        assert len(bindings) == 1
        assert bindings[0].metric == "revenue"

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_metric_bindings(tmp_path) == []

    def test_missing_required_field(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "metrics"
        d.mkdir(parents=True)
        (d / "bad.yaml").write_text("metric: revenue\n")  # missing canonical
        with pytest.raises(ContractError, match="canonical"):
            load_metric_bindings(tmp_path)

    def test_file_line_in_error(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "metrics"
        d.mkdir(parents=True)
        (d / "bad.yaml").write_text("metric: revenue\n")
        with pytest.raises(ContractError) as exc_info:
            load_metric_bindings(tmp_path)
        assert "bad.yaml" in str(exc_info.value)

    def test_ac1_duplicate_active_metric_name(self, tmp_path: Path) -> None:
        """AC1: two active bindings with the same metric name → ContractError naming both paths."""
        d = tmp_path / "contracts" / "metrics"
        d.mkdir(parents=True)
        (d / "revenue_a.yaml").write_text(VALID_BINDING_YAML)
        (d / "revenue_b.yaml").write_text(VALID_BINDING_YAML)
        with pytest.raises(ContractError) as exc_info:
            load_metric_bindings(tmp_path)
        msg = str(exc_info.value)
        assert "revenue_a.yaml" in msg
        assert "revenue_b.yaml" in msg

    def test_ac1_duplicate_via_alias(self, tmp_path: Path) -> None:
        """AC1: two active bindings where one's alias matches the other's metric name."""
        d = tmp_path / "contracts" / "metrics"
        d.mkdir(parents=True)
        (d / "revenue.yaml").write_text(VALID_BINDING_YAML)
        # Second binding whose metric collides with alias "rev" from first binding
        second = (
            "metric: rev\n"
            "canonical:\n"
            "  source: orders\n"
            "  measure: total_revenue\n"
            "provenance: human_curated\n"
        )
        (d / "rev.yaml").write_text(second)
        with pytest.raises(ContractError, match="rev"):
            load_metric_bindings(tmp_path)

    def test_deprecated_binding_not_checked_for_duplicates(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "metrics"
        d.mkdir(parents=True)
        (d / "revenue.yaml").write_text(VALID_BINDING_YAML)
        deprecated = VALID_BINDING_YAML.replace("status: active", "status: deprecated")
        (d / "revenue_old.yaml").write_text(deprecated)
        bindings = load_metric_bindings(tmp_path)
        assert len(bindings) == 2  # no error; deprecated doesn't conflict


VALID_RESTRICT_SOURCE_YAML = """\
id: board-final-only
applies_to:
  metric: revenue
kind: restrict_source
restrict_to:
  role: final
context: board_reporting
severity: error
rationale: "Board reporting requires authoritative data through T-1."
"""


class TestLoadGuardrails:
    def test_happy_path(self, tmp_contracts_dir: Path) -> None:
        guardrails = load_guardrails(tmp_contracts_dir)
        assert len(guardrails) == 1
        assert guardrails[0].id == "revenue-excludes-refunds"

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_guardrails(tmp_path) == []

    def test_finality_files_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "guardrails"
        d.mkdir(parents=True)
        (d / "finality-revenue.yaml").write_text(VALID_FINALITY_YAML)
        (d / "revenue-excludes-refunds.yaml").write_text(VALID_GUARDRAIL_YAML)
        guardrails = load_guardrails(tmp_path)
        assert len(guardrails) == 1
        assert guardrails[0].id == "revenue-excludes-refunds"

    def test_restrict_source_guardrail_loads(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "guardrails"
        d.mkdir(parents=True)
        (d / "board-reporting-final-only.yaml").write_text(VALID_RESTRICT_SOURCE_YAML)
        guardrails = load_guardrails(tmp_path)
        assert len(guardrails) == 1
        g = guardrails[0]
        assert g.id == "board-final-only"
        assert g.context == "board_reporting"
        assert g.restrict_to is not None
        assert g.restrict_to.role == "final"


class TestLoadAssertions:
    def test_happy_path(self, tmp_contracts_dir: Path) -> None:
        assertions = load_assertions(tmp_contracts_dir)
        assert len(assertions) == 1
        assert assertions[0].id == "revenue-2025-q1"

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_assertions(tmp_path) == []

    def test_p1_stub_loads_without_error(self, tmp_path: Path) -> None:
        d = tmp_path / "contracts" / "assertions"
        d.mkdir(parents=True)
        (d / "revenue-2025-q1.yaml").write_text(VALID_ASSERTION_YAML)
        assertions = load_assertions(tmp_path)
        assert assertions[0].source_of_truth == "Finance close, FY25 Q1"


class TestContractsDirScaffold:
    def test_creates_all_dirs(self, tmp_path: Path) -> None:
        contracts_dir_scaffold(tmp_path)
        assert (tmp_path / "contracts" / "metrics").is_dir()
        assert (tmp_path / "contracts" / "guardrails").is_dir()
        assert (tmp_path / "contracts" / "assertions").is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        contracts_dir_scaffold(tmp_path)
        contracts_dir_scaffold(tmp_path)  # must not raise
