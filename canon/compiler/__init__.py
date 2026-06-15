"""Deterministic compiler: semantic query → dialect-correct read-only SQL (SPEC-E5-E15 §4)."""

from __future__ import annotations

from canon.compiler.dialect import DIALECT_ADAPTERS, DialectAdapter, PostgresDialectAdapter
from canon.compiler.pipeline import compile
from canon.compiler.query import SemanticQuery
from canon.compiler.result import CompileResult, FiredGuardrail, SourceFreshness

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
