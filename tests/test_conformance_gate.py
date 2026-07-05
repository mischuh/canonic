"""Conformance gate — SPEC-P0 §5.

Golden JSON schemas are stored under tests/snapshots/contract_schema_v1/.
Any change to the frozen field set of SemanticQuery, QueryResult, CompileOutput,
or the error registry fails these tests unless the snapshot is updated *and*
contract_schema is bumped per §4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from canonic.compiler.query import SemanticQuery
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import CompileOutput, QueryResult
from canonic.exc import EXIT_CODES, ErrorCode

_SNAPSHOTS = Path(__file__).parent / "snapshots" / "contract_schema_v1"


def _load(name: str) -> object:
    return json.loads((_SNAPSHOTS / name).read_text())


@pytest.mark.release_gate
def test_semantic_query_schema_unchanged() -> None:
    assert SemanticQuery.model_json_schema() == _load("semantic_query.json"), (
        "SemanticQuery schema changed — update the snapshot and bump contract_schema per §4"
    )


@pytest.mark.release_gate
def test_query_result_schema_unchanged() -> None:
    assert QueryResult.model_json_schema() == _load("query_result.json"), (
        "QueryResult schema changed — update the snapshot and bump contract_schema per §4"
    )


@pytest.mark.release_gate
def test_compile_output_schema_unchanged() -> None:
    assert CompileOutput.model_json_schema() == _load("compile_output.json"), (
        "CompileOutput schema changed — update the snapshot and bump contract_schema per §4"
    )


@pytest.mark.release_gate
def test_error_registry_unchanged() -> None:
    registry = {code.value: EXIT_CODES[code] for code in ErrorCode}
    assert registry == _load("error_registry.json"), (
        "Error registry changed — update the snapshot and bump contract_schema per §4"
    )


@pytest.mark.release_gate
def test_contract_schema_stamped_in_snapshots() -> None:
    qr_schema = _load("query_result.json")
    co_schema = _load("compile_output.json")
    for schema in (qr_schema, co_schema):
        props = schema.get("$defs", {}).get("QueryMetadata", {}).get("properties", {})
        default = props.get("contract_schema", {}).get("default")
        assert default == CONTRACT_SCHEMA, (
            f"contract_schema default in snapshot ({default!r}) != "
            f"current CONTRACT_SCHEMA ({CONTRACT_SCHEMA!r})"
        )


_COMPILER_ROOT = Path(__file__).parent.parent / "canonic" / "compiler"
_RUNTIME_SYMBOLS = (
    "canonic.runtime",
    "GenerationRuntime",
    "EmbeddingRuntime",
    "RuntimeLLMDrafter",
)


def test_e5_compiler_never_imports_e10() -> None:
    """E5 (compiler) must never reference E10 (generation/embedding runtime).

    E5 is LLM-free by construction (SPEC-E10 §9 / GH-68): identical inputs yield
    byte-identical SQL with no randomness. This static scan locks that property so a
    future edit that pulls E10 into the compiler fails CI immediately.
    """
    violations: list[str] = []
    for py_file in sorted(_COMPILER_ROOT.rglob("*.py")):
        source = py_file.read_text()
        for symbol in _RUNTIME_SYMBOLS:
            if symbol in source:
                rel = py_file.relative_to(_COMPILER_ROOT.parent.parent)
                violations.append(f"{rel}: found {symbol!r}")
    assert not violations, "E5 compiler must not reference E10 runtime:\n" + "\n".join(violations)
