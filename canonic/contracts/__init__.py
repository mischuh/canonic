"""Contract surface: typed models and YAML IO for contracts/**/*.yaml (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

from canonic.contracts.assertions import (
    AssertionOutcome,
    assertion_to_query,
    is_executable,
    match_result,
)
from canonic.contracts.loader import (
    contracts_dir_scaffold,
    dump_assertion,
    dump_guardrail,
    dump_metric_binding,
    load_assertions,
    load_guardrails,
    load_metric_bindings,
)
from canonic.contracts.models import (
    AppliesTo,
    Assertion,
    AssertionExpect,
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
from canonic.contracts.resolver import (
    Ambiguous,
    Binding,
    ContractResolver,
    MetricResolution,
    Unresolved,
)
from canonic.contracts.validate import validate_contracts

__all__ = [
    "Ambiguous",
    "AppliesTo",
    "Assertion",
    "AssertionExpect",
    "AssertionOutcome",
    "Binding",
    "CanonicalRef",
    "ContractResolver",
    "ContractValidationError",
    "DeprecatedAlternative",
    "FinalityRule",
    "Guardrail",
    "GuardrailKind",
    "MetricBinding",
    "MetricResolution",
    "Realization",
    "Severity",
    "Status",
    "Unresolved",
    "assertion_to_query",
    "contracts_dir_scaffold",
    "dump_assertion",
    "dump_guardrail",
    "dump_metric_binding",
    "is_executable",
    "load_assertions",
    "load_guardrails",
    "load_metric_bindings",
    "match_result",
    "validate_contracts",
]
