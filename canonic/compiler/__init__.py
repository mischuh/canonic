"""Deterministic compiler: semantic query → dialect-correct read-only SQL (SPEC-E5-E15 §4)."""

from __future__ import annotations

from canonic.compiler.dialect import DIALECT_ADAPTERS, DialectAdapter, PostgresDialectAdapter
from canonic.compiler.pipeline import compile
from canonic.compiler.query import SemanticQuery
from canonic.compiler.result import CompileResult, FiredGuardrail, SourceFreshness

__all__ = [
    "DIALECT_ADAPTERS",
    "CompileResult",
    "DialectAdapter",
    "FiredGuardrail",
    "PostgresDialectAdapter",
    "SemanticQuery",
    "SourceFreshness",
    "compile",
]
