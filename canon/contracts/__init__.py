"""Contract surface: typed models and YAML IO for contracts/**/*.yaml (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

from canon.contracts.loader import (
    contracts_dir_scaffold,
    dump_assertion,
    dump_guardrail,
    dump_metric_binding,
    load_assertions,
    load_guardrails,
    load_metric_bindings,
)
from canon.contracts.models import (
    AppliesTo,
    Assertion,
    CanonicalRef,
    ContractValidationError,
    DeprecatedAlternative,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Realization,
    Severity,
    Status,
)
from canon.contracts.validate import validate_contracts

__all__ = [
    "AppliesTo",
    "Assertion",
    "CanonicalRef",
    "ContractValidationError",
    "DeprecatedAlternative",
    "FinalityRule",
    "Guardrail",
    "GuardrailKind",
    "MetricBinding",
    "Realization",
    "Severity",
    "Status",
    "contracts_dir_scaffold",
    "dump_assertion",
    "dump_guardrail",
    "dump_metric_binding",
    "load_assertions",
    "load_guardrails",
    "load_metric_bindings",
    "validate_contracts",
]
