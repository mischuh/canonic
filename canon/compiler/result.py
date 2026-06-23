"""Compiler output object — SQL plus trust/provenance metadata (SPEC-E5-E15 §4 step 8).

These attributes are consumed downstream by the trust score (E14) and the serving
surfaces (E7/E8); they are plain frozen dataclasses so the core stays protocol-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CompileResult",
    "CompositionMetadata",
    "FinalityMetadata",
    "FiredGuardrail",
    "PartialAdditiveMetadata",
    "RecomputeAtGrainMetadata",
    "SourceFreshness",
]


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
class CompositionMetadata:
    """Records how a composable_post_agg metric was produced (SPEC-Fuller-E15 §4.1, §6 stage 8).

    Consumed by E14 (trust scoring) and E16 (event log) to record that division
    was applied post-aggregation, not row-by-row.
    """

    kind: str
    numerator: str
    denominator: str
    on_zero_denominator: str


@dataclass(frozen=True, slots=True)
class PartialAdditiveMetadata:
    """Records how a semi_additive metric was compiled (SPEC-Fuller-E15 §4.2, §6 stage 8).

    ``collapsed`` is True when the window/nested-aggregate form was emitted (collapse_dimension
    was not grouped); False when the additive pass-through was used (dimension was grouped).
    """

    kind: str
    collapse_dimension: str
    collapse_agg: str
    collapsed: bool


@dataclass(frozen=True, slots=True)
class RecomputeAtGrainMetadata:
    """Records how a recompute_at_grain metric was compiled (SPEC-Fuller-E15 §4.3, §6 stage 8).

    Consumed by E14 (trust scoring) and E16 (event log) to record that the aggregation
    was recomputed from base rows at the requested grain, never derived from pre-aggregates.
    """

    kind: str
    distinct_on: str | None
    column: str | None
    quantile: float | None


@dataclass(frozen=True, slots=True)
class FinalityMetadata:
    """Finality coalescing metadata produced by compiler stage 5 (SPEC-E5-E15 §4 stage 8).

    ``watermark`` is an ISO-8601 timestamp string. ``sources_used`` are the realization
    source names that were selected for this query window. ``result_flag`` mirrors the
    contract's ``result_flag`` value (e.g. ``"per_row"``).

    Row counts (``final_rows`` / ``provisional_rows``) are populated later, after the
    ``ResultSet`` is available (in the core service layer), not at compile time.
    """

    watermark: str
    sources_used: list[str]
    result_flag: str


@dataclass(frozen=True, slots=True)
class CompileResult:
    """The compiled query and its result attributes (SPEC-E5-E15 §4).

    ``resolved`` maps each requested metric name to ``"source.measure"``. ``stale`` in
    every :class:`SourceFreshness` is ``False`` in P0 — no staleness policy is defined yet.
    ``finality`` is ``None`` when no finality rule applies; all rows are implicitly final.
    """

    sql: str
    dialect: str
    resolved: dict[str, str]
    guardrails_fired: list[FiredGuardrail] = field(default_factory=list)
    freshness: list[SourceFreshness] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finality: FinalityMetadata | None = None
    composition: CompositionMetadata | None = None
    partial_additive: PartialAdditiveMetadata | None = None
    recompute_at_grain: RecomputeAtGrainMetadata | None = None
