"""E10-backed LLM drafter for the E4 builder seam (SPEC-E10 §10, SPEC-E4 §4).

Bridges the deterministic builder's :class:`~canonic.ingestion.builder.LLMDrafter` seam to a
real :class:`~canonic.runtime.generation.GenerationRuntime`. Injected on the interactive path
to replace the headless ``NullLLMDrafter``; this is the concrete proof of SPEC-E10 S1-AC1
— an E4 draft succeeds with no engine-specific code path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from canonic.ingestion.builder import (
    LLM_GRAIN_CONFIDENCE,
    LLM_GRAIN_CONFIDENCE_CEILING,
    DimensionEnrichment,
    GrainDraft,
    JoinDraft,
    NullLLMDrafter,
)
from canonic.ingestion.reconciliation import NullReconcileDrafter, ResolutionDraft
from canonic.runtime.resolver import Task

if TYPE_CHECKING:
    from canonic.airgap import EgressPolicy
    from canonic.config import LLMConfig, RuntimeConfig
    from canonic.connectors.base import RelationSchema
    from canonic.ingestion.builder import LLMDrafter
    from canonic.ingestion.models import Proposal
    from canonic.ingestion.reconciliation import ReconcileDrafter
    from canonic.runtime.generation import GenerationRuntime

__all__ = ["RuntimeLLMDrafter", "RuntimeReconcileDrafter", "make_drafter", "make_reconcile_drafter"]

_GRAIN_SYSTEM = (
    "You are a database schema analyst. Your task is to infer the GRAIN of a relation: the "
    "minimal set of columns whose values together uniquely identify exactly one row. A correct "
    "grain has no redundant columns (removing any column would make it ambiguous) and no "
    "missing columns (some other row could otherwise share the same values). "
    "Use the column types, nullability, foreign keys, row count, and — when provided — the "
    "cardinality/null statistics to reason about which columns are candidate keys. A column "
    "with an estimated uniqueness ratio near 1.0 is a strong single-column grain candidate; a "
    "surrogate id-like column with low null fraction is preferred over nullable/low-cardinality "
    "columns. Composite grains are common in event/log and bridge/junction tables. "
    "Respond only with the requested JSON object — no prose outside it."
)

# Exactly two few-shot examples, written directly into the prompt as a module-level constant
# (not loaded from a file) so the prompt stays self-contained and diffable in code review.
_FEW_SHOT_EXAMPLES = """\
### Example 1 — event/log table with a natural composite key
Table name: 'analytics.stg_hubspot_page_views'
Kind: table
Estimated row count: 4,802,113
Columns:
- visitor_id (string, not null, uniqueness=0.041)
- page_url (string, not null, uniqueness=0.002)
- viewed_at (timestamp, not null, uniqueness=0.998)
- session_id (string, nullable, null_fraction=0.12)

Answer:
{"inferred_grain": ["visitor_id", "page_url", "viewed_at"], "confidence_score": 0.82, \
"reasoning": "No single column is unique (visitor_id and page_url both repeat heavily); the \
combination of visitor, page, and timestamp is the natural grain of a page-view event log, \
since the same visitor can view the same page at different times."}

### Example 2 — junction/bridge table
Table name: 'analytics.order_line_items'
Kind: table
Estimated row count: 918,442
Columns:
- order_id (int, not null, uniqueness=0.31)
- product_id (int, not null, uniqueness=0.05)
- quantity (int, not null)
- unit_price (decimal, not null)

Foreign keys:
- (order_id) -> analytics.orders (id)
- (product_id) -> analytics.products (id)

Answer:
{"inferred_grain": ["order_id", "product_id"], "confidence_score": 0.9, "reasoning": "This is \
a bridge table between orders and products; the two foreign-key columns together identify one \
line item, and quantity/unit_price are attributes, not identifiers."}
"""


class _GrainResponse(BaseModel):
    """Schema the model must satisfy when drafting a grain."""

    model_config = ConfigDict(populate_by_name=True)

    grain: list[str] = Field(alias="inferred_grain")
    confidence: float = Field(
        default=LLM_GRAIN_CONFIDENCE, ge=0.0, le=1.0, alias="confidence_score"
    )
    reasoning: str = ""


class RuntimeLLMDrafter:
    """A real ``LLMDrafter`` backed by the generation runtime (SPEC-E10 S1-AC1).

    Satisfies the async ``LLMDrafter`` Protocol by delegating directly to the async runtime.
    Injected on the interactive path to replace the headless ``NullLLMDrafter``; the headless
    pipeline stays fully deterministic (SPEC-E4 §9).
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft:
        """Propose a grain for a relation with no declared primary key."""
        completion = await self._runtime.generate(
            _grain_prompt(schema),
            task=Task.DRAFT,
            system=_GRAIN_SYSTEM,
            response_model=_GrainResponse,
        )
        if not completion.parsed:
            return GrainDraft(grain=[], confidence=LLM_GRAIN_CONFIDENCE, reasoning="")
        grain = completion.parsed.get("grain", [])
        raw_confidence = completion.parsed.get("confidence", LLM_GRAIN_CONFIDENCE)
        reasoning = completion.parsed.get("reasoning", "")
        confidence = min(float(raw_confidence), LLM_GRAIN_CONFIDENCE_CEILING) if grain else 0.0
        return GrainDraft(grain=grain, confidence=confidence, reasoning=reasoning)

    async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
        """Propose joins from observed-query evidence.

        Deferred to a later E4 stage; grain drafting alone exercises the seam for #61.
        """
        return []

    async def draft_dimension_labels(
        self, schema: RelationSchema, dimensions: list[dict[str, Any]]
    ) -> list[DimensionEnrichment]:
        """Propose a display label and, when confident, aliases for each dimension."""
        completion = await self._runtime.generate(
            _dimension_label_prompt(schema, dimensions),
            task=Task.DRAFT,
            system=_DIMENSION_LABEL_SYSTEM,
            response_model=_DimensionLabelResponse,
        )
        if not completion.parsed:
            return []
        entries = completion.parsed.get("dimensions", [])
        return [DimensionEnrichment.model_validate(e) for e in entries]

    async def draft_schema_joins(
        self,
        schema: RelationSchema,
        candidate_columns: list[str],
        other_relations: dict[str, RelationSchema],
    ) -> list[JoinDraft]:
        """Propose FK-less joins for candidate columns via naming convention + schema evidence."""
        completion = await self._runtime.generate(
            _schema_join_prompt(schema, candidate_columns, other_relations),
            task=Task.DRAFT,
            system=_SCHEMA_JOIN_SYSTEM,
            response_model=_SchemaJoinResponse,
        )
        if not completion.parsed:
            return []
        entries = completion.parsed.get("joins", [])
        return [JoinDraft.model_validate(e) for e in entries]


def _grain_prompt(schema: RelationSchema) -> str:
    """Render a relation's full evidence — schema, FKs, row count, and data profile — into a
    grain-inference prompt with few-shot examples.

    Every enrichment section degrades gracefully: foreign keys, row count, and the
    data-profile (cardinality/null) section are omitted entirely when the relation carries
    none (e.g. SQLite/DuckDB, or a Postgres relation with ``fetch_column_stats`` unset or
    never-ANALYZE'd) rather than rendering an empty or misleading section.
    """
    lines: list[str] = ["## Examples\n", _FEW_SHOT_EXAMPLES, "\n## Your task\n"]

    lines.append(f"Table name: {schema.relation!r}")
    lines.append(f"Kind: {schema.kind}")
    if schema.row_count_estimate is not None:
        lines.append(f"Estimated row count: {schema.row_count_estimate:,}")

    lines.append("\nColumns:")
    for c in schema.columns:
        parts = [f"- {c.name} ({c.type}", ", nullable" if c.nullable else ", not null"]
        if c.uniqueness_ratio is not None:
            parts.append(f", uniqueness={c.uniqueness_ratio:.3f}")
        if c.null_fraction is not None:
            parts.append(f", null_fraction={c.null_fraction:.3f}")
        parts.append(")")
        lines.append("".join(parts))

    if schema.foreign_keys:
        lines.append("\nForeign keys:")
        for fk in schema.foreign_keys:
            lines.append(
                f"- ({', '.join(fk.columns)}) -> "
                f"{fk.references.relation} ({', '.join(fk.references.columns)})"
            )

    if not any(c.stats_source for c in schema.columns):
        lines.append(
            "\nNote: no column-level cardinality/null statistics are available for this "
            "relation (zero-scan mode, or never analyzed) — infer the grain from names, "
            "types, nullability, and foreign keys alone."
        )

    lines.append(
        "\nReturn a JSON object with exactly these keys: "
        '"inferred_grain" (list of column names, minimal uniquely-identifying set), '
        '"confidence_score" (float 0.0-1.0, your own calibrated confidence in this grain), '
        '"reasoning" (1-3 sentences explaining why these columns and not others).'
    )
    return "\n".join(lines)


_DIMENSION_LABEL_SYSTEM = (
    "You are a data analyst helping onboard a new database into a business-facing semantic "
    "layer. For each dimension listed, propose a short, human-readable display label in Title "
    "Case (e.g. column 'product_type' -> label 'Product Type'; 'is_active' -> 'Is Active'). "
    "Only when you are genuinely confident, also propose a short list of aliases: alternative "
    "names a business user might type when searching for this concept (synonyms, common "
    "abbreviations, or the same concept phrased differently) — never invent aliases you are "
    "not sure fit, and leave the alias list empty rather than guess. Report your own calibrated "
    "confidence per dimension; a generic surrogate id or timestamp column with no clear "
    "business meaning should get a low confidence and an empty alias list. "
    "Respond only with the requested JSON object — no prose outside it."
)

_DIMENSION_LABEL_EXAMPLES = """\
### Example — table with a categorical dimension and a surrogate key
Table name: 'analytics.products'
Dimensions:
- name (string, column: name)
- type (string, column: type)
- product_id (string, column: product_id)

Answer:
{"dimensions": [
{"name": "name", "label": "Product Name", "aliases": [], "confidence": 0.6, \
"reasoning": "A generic name field; no further synonyms are safe to assume."},
{"name": "type", "label": "Product Type", "aliases": ["product_category", "category"], \
"confidence": 0.8, "reasoning": "A categorical column commonly called 'category' in \
e-commerce data."},
{"name": "product_id", "label": "Product Id", "aliases": [], "confidence": 0.3, \
"reasoning": "A surrogate key; no business synonym applies."}
]}
"""


class _DimensionLabelResponse(BaseModel):
    """Schema the model must satisfy when drafting dimension labels/aliases."""

    dimensions: list[DimensionEnrichment] = []


def _dimension_label_prompt(schema: RelationSchema, dimensions: list[dict[str, Any]]) -> str:
    """Render a relation's already-inferred dimensions into a label/alias-drafting prompt."""
    lines: list[str] = ["## Example\n", _DIMENSION_LABEL_EXAMPLES, "\n## Your task\n"]

    lines.append(f"Table name: {schema.relation!r}")
    lines.append("Dimensions:")
    for dim in dimensions:
        lines.append(f"- {dim['name']} (column: {dim['column']})")

    lines.append(
        '\nReturn a JSON object with key "dimensions": a list with one entry per dimension '
        'above, each an object with "name" (must match exactly), "label" (Title Case display '
        'name), "aliases" (list of strings, empty unless confident), "confidence" (float '
        '0.0-1.0), and "reasoning" (one sentence).'
    )
    return "\n".join(lines)


_SCHEMA_JOIN_SYSTEM = (
    "You are a data warehouse analyst reviewing a database that declares no foreign-key "
    "constraints, even though a star or snowflake schema exists in practice. For each "
    "candidate column on the table under review, decide whether it is very likely a pointer "
    "to one of the other listed tables (a star-schema fact table referencing a dimension via "
    "a key column, or a snowflake-schema dimension rolling up to a higher-aggregate "
    "dimension), based only on column-name convention and the columns each candidate target "
    "table declares. Propose a join only when a target table and target column are a strong, "
    "unambiguous match; skip a candidate column entirely rather than guess when no table name "
    "clearly corresponds to it, or when it could plausibly match more than one target equally "
    "well. Report your own calibrated confidence per proposed join. "
    "Respond only with the requested JSON object — no prose outside it."
)

_SCHEMA_JOIN_EXAMPLES = """\
### Example — star-schema fact table and a snowflake dimension rollup
Table name: 'analytics.fct_orders'
Candidate columns (no declared foreign key): customer_key, category_key
Other tables in this connection:
- dim_customers: customer_key, name, signup_date
- dim_products: product_key, name, category_key
- dim_categories: category_key, label

Answer:
{"joins": [
{"column": "customer_key", "to": "dim_customers", "to_column": "customer_key", \
"confidence": 0.85, "reasoning": "dim_customers declares the same customer_key column and \
its name matches the target table exactly."},
{"column": "category_key", "to": "dim_categories", "to_column": "category_key", \
"confidence": 0.5, "reasoning": "Both dim_products and dim_categories declare category_key, \
but dim_categories is the clearer target since its own primary concept is the category."}
]}
"""


class _SchemaJoinResponse(BaseModel):
    """Schema the model must satisfy when drafting FK-less joins."""

    joins: list[JoinDraft] = []


def _schema_join_prompt(
    schema: RelationSchema,
    candidate_columns: list[str],
    other_relations: dict[str, RelationSchema],
) -> str:
    """Render a relation's FK-less candidate columns and sibling tables into a join-drafting
    prompt — only column names and types are shared for each candidate target, not full
    schemas, keeping the prompt compact even with many tables in the same connection.
    """
    lines: list[str] = ["## Example\n", _SCHEMA_JOIN_EXAMPLES, "\n## Your task\n"]

    lines.append(f"Table name: {schema.relation!r}")
    lines.append(f"Candidate columns (no declared foreign key): {', '.join(candidate_columns)}")

    lines.append("Other tables in this connection:")
    for rel_name, rel_schema in other_relations.items():
        columns = ", ".join(c.name for c in rel_schema.columns)
        lines.append(f"- {rel_name}: {columns}")

    lines.append(
        '\nReturn a JSON object with key "joins": a list with at most one entry per candidate '
        'column that has a strong match, each an object with "column" (must match one of the '
        'candidate columns above), "to" (must match one of the other table names above), '
        '"to_column" (must be a column that table declares above), "confidence" (float '
        '0.0-1.0), and "reasoning" (one sentence). Omit a candidate column entirely instead of '
        "guessing when no target is a strong, unambiguous match."
    )
    return "\n".join(lines)


_RECONCILE_SYSTEM = (
    "You resolve contradictions between two proposed descriptions of the same database object. "
    "Given two proposals, select the one that is most accurate and complete. "
    "Respond only with the requested JSON."
)


class _ResolutionResponse(BaseModel):
    """Schema the model must satisfy when resolving a contradiction."""

    winner_index: int


class RuntimeReconcileDrafter:
    """A real ``ReconcileDrafter`` backed by the generation runtime (SPEC-E10 S2-AC1).

    Presents the conflicting proposals to the stronger ``reconcile``-task model and returns
    the winning index. Injected on the interactive path to replace ``NullReconcileDrafter``.
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    async def draft_resolution(
        self, target: str, proposals: list[Proposal]
    ) -> ResolutionDraft | None:
        """Ask the model to pick the winning proposal for a contradicting group."""
        completion = await self._runtime.generate(
            _resolution_prompt(target, proposals),
            task=Task.RECONCILE,
            system=_RECONCILE_SYSTEM,
            response_model=_ResolutionResponse,
        )
        if not completion.parsed:
            return None
        winner_index = completion.parsed.get("winner_index")
        if not isinstance(winner_index, int) or not (0 <= winner_index < len(proposals)):
            return None
        return ResolutionDraft(winner_index=winner_index)


def _resolution_prompt(target: str, proposals: list[Proposal]) -> str:
    """Render the conflicting proposals into a resolution prompt."""
    lines = [f"Two sources disagree on the description of {target!r}.", ""]
    for i, proposal in enumerate(proposals):
        lines.append(f"Proposal {i}:")
        lines.append(json.dumps(proposal.content, indent=2, default=str))
        lines.append("")
    lines.append(
        'Return the index of the better proposal as JSON: {"winner_index": 0} or {"winner_index": 1}.'
    )
    return "\n".join(lines)


def make_drafter(
    llm: LLMConfig | None,
    runtime: RuntimeConfig,
    *,
    headless: bool,
) -> LLMDrafter:
    """Return the right LLMDrafter for the operating mode (SPEC-E10 §9).

    Headless or no LLM configured → NullLLMDrafter (zero model calls, fully deterministic).
    Interactive with LLM → RuntimeLLMDrafter backed by GenerationRuntime.
    Air-gapped policy is threaded into the runtime so the egress guard fires at
    construction time (before any call) even in interactive mode.
    """
    if headless or llm is None:
        return NullLLMDrafter()
    from canonic.airgap import EgressPolicy
    from canonic.runtime.generation import GenerationRuntime

    policy: EgressPolicy | None = (
        EgressPolicy(allow_cidrs=runtime.allow_cidrs) if runtime.air_gapped else None
    )
    return RuntimeLLMDrafter(GenerationRuntime(llm, policy=policy))


def make_reconcile_drafter(
    llm: LLMConfig | None,
    runtime: RuntimeConfig,
    *,
    headless: bool,
) -> ReconcileDrafter:
    """Return the right ReconcileDrafter for the operating mode (SPEC-E10 §9).

    Headless or no LLM configured → NullReconcileDrafter (no model calls).
    Interactive with LLM → RuntimeReconcileDrafter backed by GenerationRuntime.
    """
    if headless or llm is None:
        return NullReconcileDrafter()
    from canonic.airgap import EgressPolicy
    from canonic.runtime.generation import GenerationRuntime

    policy: EgressPolicy | None = (
        EgressPolicy(allow_cidrs=runtime.allow_cidrs) if runtime.air_gapped else None
    )
    return RuntimeReconcileDrafter(GenerationRuntime(llm, policy=policy))
