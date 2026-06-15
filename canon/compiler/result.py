"""Compiler output object — SQL plus trust/provenance metadata (SPEC-E5-E15 §4 step 8).

These attributes are consumed downstream by the trust score (E14) and the serving
surfaces (E7/E8); they are plain frozen dataclasses so the core stays protocol-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CompileResult", "FiredGuardrail", "SourceFreshness"]


@dataclass(frozen=True, slots=True)
class FiredGuardrail:
    """A guardrail that the compiler enforced on this query."""

    id: str
    kind: str


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """Per-source freshness drawn from each used source's ``meta.last_validated_at``."""

    source: str
    last_validated_at: str | None
    stale: bool


@dataclass(frozen=True, slots=True)
class CompileResult:
    """The compiled query and its result attributes (SPEC-E5-E15 §4).

    ``resolved`` maps each requested metric name to ``"source.measure"``. ``stale`` in
    every :class:`SourceFreshness` is ``False`` in P0 — no staleness policy is defined yet.
    """

    sql: str
    dialect: str
    resolved: dict[str, str]
    guardrails_fired: list[FiredGuardrail] = field(default_factory=list)
    freshness: list[SourceFreshness] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
