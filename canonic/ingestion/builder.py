"""Context builder — normalized evidence → Proposal[] (SPEC-E4 §4).

Stage 1 of the ingestion pipeline. Turns each ``EvidenceItem`` into one or more
``Proposal`` objects without touching any committed file. The deterministic core
maps a ``RelationSchema`` directly to a ``semantics/<conn>/<name>.yaml`` draft and is
the only builder path in headless mode (SPEC-E4 §9). LLM-assisted drafting (grain
without a primary key, joins from observed queries) is a parallel sub-track gated
behind an injected ``LLMDrafter``; the default is a deterministic null stub.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from canonic.connectors.base import (
    AcquisitionTier,
    ColumnInfo,
    DefinitionEntityType,
    ForeignKey,
    RelationSchema,
    UsageEvidence,
    UsageRole,
)
from canonic.ingestion.models import (
    DraftedBy,
    EvidenceItem,
    EvidenceKind,
    Proposal,
    ProposalOp,
)
from canonic.semantic.models import Provenance, Relationship

__all__ = [
    "BuildResult",
    "ContextBuilder",
    "DimensionEnrichment",
    "GrainDraft",
    "JoinDraft",
    "LLMDrafter",
    "NullLLMDrafter",
    "SkippedEvidence",
]

# Confidence the builder assigns to its two drafting origins (SPEC-E4 §4): a
# deterministic inference is fully trusted; an LLM-drafted grain is a labelled
# candidate carrying lower certainty so it sorts behind deterministic facts in review.
DETERMINISTIC_CONFIDENCE = 1.0
LLM_GRAIN_CONFIDENCE = 0.3

# Ceiling applied to a drafter's self-reported confidence (SPEC-E10 grain-quality rework):
# grain proposals are unconditionally excluded from auto-apply (AutoApplyConfig.never), so a
# self-reported score is safe to surface, but small local models are frequently overconfident —
# capping below DETERMINISTIC_CONFIDENCE keeps LLM-drafted grains visually distinguishable from
# deterministic ones in review-queue sorting.
LLM_GRAIN_CONFIDENCE_CEILING = 0.85

# Confidence thresholds gating LLM-drafted dimension labels/aliases (bootstrap task
# expansion): a label is cosmetic formatting, so it's applied whenever the model is
# reasonably sure; an alias is a factual claim consumed by MCP retrieval, so it needs a
# stricter bar — a wrong alias silently misroutes a lookup, a wrong label is just ugly.
LLM_LABEL_CONFIDENCE_THRESHOLD = 0.5
LLM_ALIAS_CONFIDENCE_THRESHOLD = 0.75

# Confidence gating for LLM-drafted FK-less joins (star/snowflake schemas with no declared
# FK constraint): below-threshold guesses are dropped outright, the rest capped like grain.
# Unlike labels/aliases, accepting a join always forces drafted_by/confidence down (see
# _build_relation_schema) — a wrong join corrupts every query that uses it via fanout, so it
# must never bypass review regardless of how confident the model claims to be.
LLM_JOIN_CONFIDENCE_THRESHOLD = 0.5
LLM_JOIN_CONFIDENCE_CEILING = 0.85

# Surrogate-key-like column names shared by measure/dimension/join inference. The measures
# duty excludes a bare "id" too (never summable); the dimension/join duty only excludes the
# suffix form (a bare "id" is normally this table's own key, not a pointer elsewhere, so it
# can still be a useful dimension — existing behavior, preserved as-is).
_SURROGATE_KEY_RE = re.compile(r"(^id$|_(id|fk|key)$)", re.IGNORECASE)
_ID_SUFFIX_RE = re.compile(r"_(id|fk|key)$", re.IGNORECASE)

# Sentinel key in a proposal's content that signals an additive deprecated-alternative
# merge rather than a full MetricBinding replacement (SPEC-E3 §3.3, E15 §2.2).
# Reconciliation detects this key and performs an idempotent append to the target binding's
# deprecated_alternatives list without touching canonical (FR-13).
_DA_SENTINEL = "_deprecated_alternative"

# Sentinel key marking an E11 outcome-evidence proposal (SPEC-E11 §4). Reconciliation detects
# this key and always decides CONTRADICTION — never ADD/EDIT — since the proposal carries no
# corrected definition, only a wrong_definition pattern for a human to review (S3-AC2).
_ANSWER_OUTCOME_SENTINEL = "_answer_outcome_evidence"


def _metric_slug(title: str) -> str:
    """Derive a ``contracts/metrics/<slug>.yaml`` filename from a question title.

    Normalises to lowercase, replaces non-alphanumeric runs with underscores, and
    caps the result at 64 characters so filenames stay filesystem-safe.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug[:64] if slug else "unknown"


def _assertion_slug(source: str, artifact: str) -> str:
    """Derive an assertion id from the connector source and artifact identifier."""
    combined = f"{source}_{artifact}"
    return re.sub(r"[^a-z0-9]+", "_", combined.lower()).strip("_")[:64]


class SkippedEvidence(BaseModel):
    """One evidence item the builder did not turn into a proposal (SPEC-E4 §3).

    An unknown ``kind`` is recorded here and skipped, never guessed at; a known kind
    without a builder handler yet is recorded the same way. Skipping is never an error.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    kind: str
    reason: str


class BuildResult(BaseModel):
    """Stateless output of one builder run — proposals plus a skip ledger (SPEC-E4 §4)."""

    model_config = ConfigDict(frozen=True)

    proposals: list[Proposal] = []
    skipped: list[SkippedEvidence] = []


class GrainDraft(BaseModel):
    """An LLM-drafted grain candidate for a relation with no declared primary key."""

    model_config = ConfigDict(frozen=True)

    grain: list[str] = []
    confidence: float = LLM_GRAIN_CONFIDENCE
    reasoning: str = ""


class DimensionEnrichment(BaseModel):
    """An LLM-drafted label/alias candidate for one already-inferred dimension.

    ``name`` matches a dimension emitted by :meth:`ContextBuilder._infer_dimensions`;
    ``confidence`` gates whether ``label``/``aliases`` are applied at all (SPEC-E4 §4
    bootstrap task expansion) — an unresolved match or a below-threshold confidence
    leaves the dimension exactly as the deterministic core produced it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    label: str | None = None
    aliases: list[str] = []
    confidence: float = 0.0
    reasoning: str = ""


class JoinDraft(BaseModel):
    """An LLM-drafted FK-less join candidate for one column with no declared foreign key.

    ``column`` is local to the relation being built; ``to``/``to_column`` name the guessed
    target relation and its referenced column. Never trusted blindly — the builder
    revalidates both against the other relations known in the same evidence batch before
    use (:meth:`ContextBuilder._draft_schema_joins`).
    """

    model_config = ConfigDict(frozen=True)

    column: str
    to: str
    to_column: str
    confidence: float = 0.0
    reasoning: str = ""


class LLMDrafter(Protocol):
    """The fuzzy-drafting seam (SPEC-E4 §4) — injected so the headless path stays LLM-free.

    Implementations propose the parts the deterministic core cannot assert: a grain when
    no primary key is declared, joins inferred from observed-query evidence, human-readable
    labels/aliases for inferred dimensions, and FK-less joins guessed from column-name
    convention across the relations in the same bootstrap batch. Every result is labelled
    ``drafted_by: llm`` by the builder and carries reduced confidence.
    """

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft:
        """Propose a grain for a relation that declares no primary key."""
        ...

    async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
        """Propose joins from an ``observed_query`` payload."""
        ...

    async def draft_dimension_labels(
        self, schema: RelationSchema, dimensions: list[dict[str, Any]]
    ) -> list[DimensionEnrichment]:
        """Propose a display label and, when confident, aliases for each dimension."""
        ...

    async def draft_schema_joins(
        self,
        schema: RelationSchema,
        candidate_columns: list[str],
        other_relations: dict[str, RelationSchema],
    ) -> list[JoinDraft]:
        """Propose FK-less joins for candidate columns via naming convention + schema evidence."""
        ...


class NullLLMDrafter:
    """Default LLM stub for the headless path — proposes nothing, asserts nothing.

    Returns an empty grain candidate (so the proposal labels grain as a draft rather than
    silently asserting one, SPEC-E4 §4 / S1-AC2), no joins, no dimension enrichment, and no
    schema-based join guesses. A real LLM drafter (E10) is injected to replace it in
    interactive mode.
    """

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft:  # noqa: ARG002 — stub
        return GrainDraft(grain=[], confidence=LLM_GRAIN_CONFIDENCE)

    async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
        return []

    async def draft_dimension_labels(
        self,
        schema: RelationSchema,  # noqa: ARG002 — stub
        dimensions: list[dict[str, Any]],  # noqa: ARG002 — stub
    ) -> list[DimensionEnrichment]:
        return []

    async def draft_schema_joins(
        self,
        schema: RelationSchema,  # noqa: ARG002 — stub
        candidate_columns: list[str],  # noqa: ARG002 — stub
        other_relations: dict[str, RelationSchema],  # noqa: ARG002 — stub
    ) -> list[JoinDraft]:
        return []


class ContextBuilder:
    """Builds ``Proposal[]`` from normalized evidence (SPEC-E4 §4).

    Stateless and free of file I/O: ``build`` is a pure function of its evidence input
    (and the injected drafter). With the default ``NullLLMDrafter`` the run is fully
    deterministic — identical evidence yields identical proposals (SPEC-E4 §9, S9-AC1).
    """

    def __init__(self, llm_drafter: LLMDrafter | None = None) -> None:
        self._llm_drafter: LLMDrafter = llm_drafter or NullLLMDrafter()

    async def build(self, evidence: list[EvidenceItem]) -> BuildResult:
        """Turn evidence into proposals, recording anything it cannot handle.

        Iterates in input order for determinism. Unknown kinds — and known kinds without
        a handler yet — are recorded in ``skipped`` and never raise (SPEC-E4 §3, S1-AC4).

        Modeling-tier MEASURE DefinitionEvidence is pre-collected in a first pass so that
        when a matching RELATION_SCHEMA is processed the builder can use the business-named
        measures (e.g. ``revenue``, ``order_count``) instead of generic inferred ones
        (``total_amount``, ``row_count``).  The short relation name (last dotted segment)
        is used as the lookup key so both ``main.orders`` and ``orders`` hit the same entry.

        Every ``RelationSchema`` in the batch is also pre-collected the same way, so a
        relation with no declared FK constraints can be matched against the other tables
        introspected in this same run when guessing FK-less joins (SPEC-E4 §4 bootstrap
        task expansion) — no ordering dependency on another relation's own drafted grain,
        since only its declared columns are needed, not its resolved grain.
        """
        named_measures: dict[str, list[dict[str, Any]]] = {}
        named_grains: dict[str, list[str]] = {}
        all_relations: dict[str, RelationSchema] = {}
        for item in evidence:
            if item.kind == EvidenceKind.RELATION_SCHEMA:
                relation_schema = RelationSchema.model_validate(item.payload)
                all_relations[relation_schema.relation.split(".")[-1]] = relation_schema
                continue
            if item.kind != EvidenceKind.DEFINITION:
                continue
            if item.acquisition_tier != AcquisitionTier.MODELING:
                continue
            payload = item.payload
            entity_type = payload.get("entity_type")
            if entity_type == DefinitionEntityType.MEASURE:
                entry: dict[str, Any] = {
                    "name": payload["entity"],
                    "expr": payload.get("expr") or payload["entity"],
                    "additivity": payload.get("additivity") or "unknown",
                }
                for ref in payload.get("references", []):
                    named_measures.setdefault(ref.split(".")[-1], []).append(entry)
            elif entity_type == DefinitionEntityType.ENTITY:
                grain = payload.get("grain") or []
                if grain:
                    for ref in payload.get("references", []):
                        named_grains.setdefault(ref.split(".")[-1], grain)

        proposals: list[Proposal] = []
        skipped: list[SkippedEvidence] = []

        for item in evidence:
            if not item.is_known():
                skipped.append(
                    SkippedEvidence(
                        source=item.source, kind=item.kind, reason="unknown evidence kind"
                    )
                )
                continue
            if item.kind == EvidenceKind.RELATION_SCHEMA:
                schema = RelationSchema.model_validate(item.payload)
                rel_key = schema.relation.split(".")[-1]
                proposals.append(
                    await self._build_relation_schema(
                        item,
                        named_measures.get(rel_key),
                        named_grains.get(rel_key),
                        all_relations,
                    )
                )
            elif item.kind == EvidenceKind.USAGE_EVIDENCE:
                proposals.extend(self._build_usage_evidence(item))
            elif item.kind == EvidenceKind.ANSWER_OUTCOME:
                proposals.append(self._build_answer_outcome(item))
            elif item.kind == EvidenceKind.DEFINITION:
                pass  # consumed by the pre-collection pass above
            else:
                skipped.append(
                    SkippedEvidence(
                        source=item.source,
                        kind=item.kind,
                        reason="no handler yet (deferred to a later E4 stage)",
                    )
                )

        return BuildResult(proposals=proposals, skipped=skipped)

    async def _build_relation_schema(
        self,
        item: EvidenceItem,
        named_measures: list[dict[str, Any]] | None = None,
        named_grain: list[str] | None = None,
        all_relations: dict[str, RelationSchema] | None = None,
    ) -> Proposal:
        """Map one ``RelationSchema`` to a ``semantics/<conn>/<name>.yaml`` draft proposal.

        With a declared primary key the grain is asserted deterministically with full
        confidence. Without one but with modeling-tier ENTITY evidence (e.g. a dbt semantic
        model primary entity), the entity-sourced grain is used deterministically.
        Otherwise the grain is an LLM-drafted candidate: labelled ``drafted_by: llm``,
        carrying reduced confidence, with the grain marked as a draft in ``meta`` rather
        than silently asserted (SPEC-E4 §4, S1-AC2).

        When ``named_measures`` is supplied (pre-collected from modeling-tier MEASURE
        DefinitionEvidence), those measures replace the generic column-inferred ones so
        the emitted semantic source carries business-meaningful names from a dbt semantic
        model rather than ``total_amount`` / ``row_count`` fallbacks.
        """
        schema = RelationSchema.model_validate(item.payload)
        name = schema.relation.split(".")[-1]

        meta: dict[str, Any] = {"source_fingerprint": schema.source_fingerprint}

        if schema.primary_key:
            grain = list(schema.primary_key)
            drafted_by = DraftedBy.DETERMINISTIC
            confidence = DETERMINISTIC_CONFIDENCE
        elif named_grain:
            grain = list(named_grain)
            drafted_by = DraftedBy.DETERMINISTIC
            confidence = DETERMINISTIC_CONFIDENCE
        else:
            draft = await self._llm_drafter.draft_grain(schema)
            grain = list(draft.grain)
            meta["grain_draft"] = True
            if draft.reasoning:
                meta["grain_reasoning"] = draft.reasoning
            drafted_by = DraftedBy.LLM
            confidence = draft.confidence

        if named_measures is not None:
            measures: list[dict[str, Any]] = named_measures
        else:
            measures = ContextBuilder._infer_measures(schema.columns)

        dimensions = ContextBuilder._infer_dimensions(schema.columns)
        await self._enrich_dimensions(schema, dimensions)

        candidate_columns = self._join_candidate_columns(schema)
        other_relations = {
            rel_name: rel_schema
            for rel_name, rel_schema in (all_relations or {}).items()
            if rel_name != name and rel_schema.connection == schema.connection
        }
        join_drafts = await self._draft_schema_joins(schema, candidate_columns, other_relations)
        if join_drafts:
            meta["join_draft"] = True
            meta["join_reasoning"] = [
                {
                    "column": d.column,
                    "to": d.to,
                    "confidence": d.confidence,
                    "reasoning": d.reasoning,
                }
                for d in join_drafts
            ]
            # Safety downgrade (never optional): a guessed join must never let this
            # proposal pass first_run_auto_acceptable's drafted_by/confidence check
            # (pipeline.py) — a wrong join corrupts every query that fans out through it,
            # so it is always routed through the same review path as an uncertain grain.
            confidence = min(confidence, min(d.confidence for d in join_drafts))
            drafted_by = DraftedBy.LLM

        content: dict[str, Any] = {
            "name": name,
            "connection": schema.connection,
            "table": schema.relation,
            "grain": grain,
            "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable} for c in schema.columns
            ],
            "measures": measures,
            "dimensions": dimensions,
            "joins": self._build_joins(name, schema.foreign_keys, join_drafts),
            "meta": meta,
        }

        return Proposal(
            target=f"semantics/{schema.connection}/{name}.yaml",
            op=ProposalOp.ADD,
            content=content,
            provenance=Provenance.INFERRED,
            confidence=confidence,
            anchored_to=[schema.source_fingerprint] if schema.source_fingerprint else [],
            drafted_by=drafted_by,
            acquisition_tier=schema.acquisition_tier,
        )

    @staticmethod
    def _build_joins(
        this_name: str,
        foreign_keys: list[ForeignKey],
        drafted: list[JoinDraft] | None = None,
    ) -> list[dict[str, Any]]:
        """Build join fragments, adding a disambiguating ``name`` when two joins share a target.

        When a table has multiple joins pointing at the same target (e.g. pickup_location_id
        and return_location_id both → locations) — whether both FK-declared, both LLM-drafted,
        or one of each — the default alias would collide. In that case a unique alias is
        derived from the join column by stripping common id/fk/key suffixes.
        """
        from collections import Counter

        drafted = drafted or []
        target_counts = Counter(
            [fk.references.relation.split(".")[-1] for fk in foreign_keys] + [d.to for d in drafted]
        )
        joins = [
            ContextBuilder._fk_to_join(
                this_name, fk, needs_alias=target_counts[fk.references.relation.split(".")[-1]] > 1
            )
            for fk in foreign_keys
        ]
        joins.extend(
            ContextBuilder._draft_to_join(this_name, d, needs_alias=target_counts[d.to] > 1)
            for d in drafted
        )
        return joins

    @staticmethod
    def _fk_to_join(this_name: str, fk: ForeignKey, *, needs_alias: bool = False) -> dict[str, Any]:
        """Project a discovered foreign key into a semantic join fragment.

        A foreign key points many local rows at one referenced row, so the relationship
        is ``many_to_one``. The ``on`` clause is built column-by-column (AND-joined for
        composite keys) in declaration order, keeping the output deterministic.
        """
        to = fk.references.relation.split(".")[-1]
        on = " AND ".join(
            f"{this_name}.{col} = {to}.{ref}"
            for col, ref in zip(fk.columns, fk.references.columns, strict=False)
        )
        result: dict[str, Any] = {
            "to": to,
            "on": on,
            "relationship": Relationship.MANY_TO_ONE.value,
        }
        if needs_alias:
            # Derive alias from the first FK column; strip trailing _id/_fk/_key suffixes.
            col_name = fk.columns[0] if fk.columns else to
            alias = re.sub(r"_(id|fk|key)$", "", col_name, flags=re.IGNORECASE)
            result["name"] = alias
        return result

    @staticmethod
    def _draft_to_join(
        this_name: str, draft: JoinDraft, *, needs_alias: bool = False
    ) -> dict[str, Any]:
        """Project a validated LLM join guess into a semantic join fragment.

        Same shape and ``many_to_one`` assumption as an FK-derived join (a star-schema
        fact→dimension pointer and a snowflake dimension→higher-aggregate-dimension rollup
        are both many-to-one) — the only difference is provenance, tracked separately via
        ``meta["join_draft"]`` on the enclosing proposal, not on the fragment itself.
        """
        result: dict[str, Any] = {
            "to": draft.to,
            "on": f"{this_name}.{draft.column} = {draft.to}.{draft.to_column}",
            "relationship": Relationship.MANY_TO_ONE.value,
        }
        if needs_alias:
            alias = re.sub(r"_(id|fk|key)$", "", draft.column, flags=re.IGNORECASE)
            result["name"] = alias
        return result

    @staticmethod
    def _join_candidate_columns(schema: RelationSchema) -> list[str]:
        """Columns that look FK-like by naming convention but carry no declared FK constraint.

        These are the candidates offered to the LLM for FK-less join guessing. Grain/PK
        columns are not excluded: a bridge table's grain is often exactly its FK-like column
        pair when the source database enforces no FK constraints at all.
        """
        declared = {col for fk in schema.foreign_keys for col in fk.columns}
        return [
            c.name
            for c in schema.columns
            if _ID_SUFFIX_RE.search(c.name) and c.name not in declared
        ]

    async def _draft_schema_joins(
        self,
        schema: RelationSchema,
        candidate_columns: list[str],
        other_relations: dict[str, RelationSchema],
    ) -> list[JoinDraft]:
        """Ask the drafter for FK-less joins inferred from column-name convention.

        A no-op — no drafter call at all — when there are no candidate columns or no other
        relations in the batch to match against (mirrors :meth:`_enrich_dimensions`'s early
        return), so :class:`NullLLMDrafter` and the headless path never pay for a call that
        cannot produce anything. Every returned candidate is revalidated against
        ``other_relations`` before use: the referenced relation and column must both exist,
        and the referenced local column must be one of the candidates actually offered — an
        LLM response is never trusted blindly (SkippedEvidence philosophy, SPEC-E4 §3: a
        hallucinated target is silently dropped, never raised).
        """
        if not candidate_columns or not other_relations:
            return []
        drafts = await self._llm_drafter.draft_schema_joins(
            schema, candidate_columns, other_relations
        )
        valid: list[JoinDraft] = []
        for draft in drafts:
            if draft.confidence < LLM_JOIN_CONFIDENCE_THRESHOLD:
                continue
            if draft.column not in candidate_columns:
                continue
            target = other_relations.get(draft.to)
            if target is None:
                continue
            if not any(c.name == draft.to_column for c in target.columns):
                continue
            valid.append(
                JoinDraft(
                    column=draft.column,
                    to=draft.to,
                    to_column=draft.to_column,
                    confidence=min(draft.confidence, LLM_JOIN_CONFIDENCE_CEILING),
                    reasoning=draft.reasoning,
                )
            )
        return valid

    @staticmethod
    def _infer_measures(columns: list[ColumnInfo]) -> list[dict[str, Any]]:
        """Derive additive measures deterministically from column types.

        Always emits ``row_count`` (count(*)) plus a ``total_<col>`` sum for every
        numeric column that is not a surrogate key (plain ``id`` or ``*_id``/``*_fk``/
        ``*_key`` suffixes).  All results carry ``additivity: additive`` so they are
        immediately p0-compilable and MCP-servable after bootstrap.
        """
        _SUMMABLE = {"int", "float", "decimal"}
        measures: list[dict[str, Any]] = [
            {"name": "row_count", "expr": "count(*)", "additivity": "additive"}
        ]
        for col in columns:
            if col.type in _SUMMABLE and not _SURROGATE_KEY_RE.search(col.name):
                measures.append(
                    {
                        "name": f"total_{col.name}",
                        "expr": f"sum({col.name})",
                        "additivity": "additive",
                    }
                )
        return measures

    @staticmethod
    def _infer_dimensions(columns: list[ColumnInfo]) -> list[dict[str, Any]]:
        """Derive categorical dimensions deterministically from column types.

        Date/timestamp columns become time dimensions; bool and string columns become
        categorical dimensions.  String columns that look like surrogate keys
        (``*_id``/``*_fk``/``*_key`` suffixes) are excluded because they are join
        keys, not useful group-by attributes.
        """
        dimensions: list[dict[str, Any]] = []
        for col in columns:
            if col.type in {"date", "timestamp", "bool"} or (
                col.type == "string" and not _ID_SUFFIX_RE.search(col.name)
            ):
                dimensions.append({"name": col.name, "column": col.name})
        return dimensions

    async def _enrich_dimensions(
        self, schema: RelationSchema, dimensions: list[dict[str, Any]]
    ) -> None:
        """Fill in LLM-drafted ``label``/``aliases`` on ``dimensions`` in place.

        A no-op with :class:`NullLLMDrafter` (headless/CI stays fully deterministic,
        SPEC-E4 §9). A label is applied once the drafter clears
        ``LLM_LABEL_CONFIDENCE_THRESHOLD``; aliases need the stricter
        ``LLM_ALIAS_CONFIDENCE_THRESHOLD`` since a wrong alias can misroute a lookup,
        while a wrong label is merely cosmetic. Unlike grain, this never changes the
        proposal's own ``drafted_by``/``confidence`` — labels/aliases are additive
        content, not a structural fact the review flow needs to gate on.
        """
        if not dimensions:
            return
        enrichments = await self._llm_drafter.draft_dimension_labels(schema, dimensions)
        drafts = {d.name: d for d in enrichments}
        for dim in dimensions:
            draft = drafts.get(dim["name"])
            if draft is None:
                continue
            if draft.label and draft.confidence >= LLM_LABEL_CONFIDENCE_THRESHOLD:
                dim["label"] = draft.label
            if draft.aliases and draft.confidence >= LLM_ALIAS_CONFIDENCE_THRESHOLD:
                dim["aliases"] = list(draft.aliases)

    def _build_usage_evidence(self, item: EvidenceItem) -> list[Proposal]:
        """Map one ``UsageEvidence`` to a proposal against the contracts surface (SPEC-E3 §3.3).

        ``role: alternative`` → an additive ``deprecated_alternative`` patch against
        ``contracts/metrics/<slug>.yaml`` (detected by the :data:`_DA_SENTINEL` key).
        Reconciliation merges the entry into the existing binding's
        ``deprecated_alternatives`` list without touching ``canonical`` (FR-13).

        ``role: trusted_example`` → a full :class:`Assertion` candidate added at
        ``contracts/assertions/<id>.yaml``.  Expected values are left empty for human
        completion; the assertion id and source are derived deterministically.

        Neither path produces a ``CanonicalRef`` — the builder-level FR-13 guarantee.
        """
        payload = UsageEvidence.model_validate(item.payload)
        fingerprint = item.source_fingerprint

        if payload.role is UsageRole.ALTERNATIVE:
            slug = _metric_slug(payload.title)
            content: dict[str, Any] = {
                _DA_SENTINEL: {
                    "source": payload.source,
                    "ref": payload.artifact,
                    "reason": payload.title,
                }
            }
            return [
                Proposal(
                    target=f"contracts/metrics/{slug}.yaml",
                    op=ProposalOp.EDIT,
                    content=content,
                    provenance=Provenance.INFERRED,
                    confidence=DETERMINISTIC_CONFIDENCE,
                    anchored_to=[fingerprint] if fingerprint else [],
                    drafted_by=DraftedBy.DETERMINISTIC,
                    acquisition_tier=item.acquisition_tier,
                )
            ]

        if payload.role is UsageRole.TRUSTED_EXAMPLE:
            assertion_slug = _assertion_slug(payload.source, payload.artifact)
            assertion_id = f"usage-{assertion_slug}"
            assertion_content: dict[str, Any] = {
                "id": assertion_id,
                "query": {
                    "native": payload.defines.expr,
                    "references": list(payload.defines.references),
                },
                "expect": {},
                "source_of_truth": payload.native_ref,
            }
            return [
                Proposal(
                    target=f"contracts/assertions/{assertion_id}.yaml",
                    op=ProposalOp.ADD,
                    content=assertion_content,
                    provenance=Provenance.INFERRED,
                    confidence=DETERMINISTIC_CONFIDENCE,
                    anchored_to=[fingerprint] if fingerprint else [],
                    drafted_by=DraftedBy.DETERMINISTIC,
                    acquisition_tier=item.acquisition_tier,
                )
            ]

        return []

    @staticmethod
    def _build_answer_outcome(item: EvidenceItem) -> Proposal:
        """Map one outcome-evidence item to a review-flagging proposal (SPEC-E11 §4).

        The payload (minted by :func:`canonic.feedback.evidence.outcome_evidence`) carries no
        corrected definition — only the pattern of ``wrong_definition`` outcomes (refs, count,
        window) that crossed the gate. The content is wrapped in the ``_ANSWER_OUTCOME_SENTINEL``
        key so reconciliation always decides CONTRADICTION for it (§4, S2-AC2), regardless of the
        existing binding's provenance tier — E11 only ever flags, it never edits (S3-AC2).
        """
        payload = item.payload
        metric = str(payload["metric"])
        refs = [str(ref) for ref in payload.get("refs", [])]
        return Proposal(
            target=f"contracts/metrics/{_metric_slug(metric)}.yaml",
            op=ProposalOp.EDIT,
            content={_ANSWER_OUTCOME_SENTINEL: dict(payload)},
            provenance=Provenance.INFERRED,
            confidence=DETERMINISTIC_CONFIDENCE,
            anchored_to=refs,
            drafted_by=DraftedBy.DETERMINISTIC,
            acquisition_tier=item.acquisition_tier,
        )
