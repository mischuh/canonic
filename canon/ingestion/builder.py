"""Context builder — normalized evidence → Proposal[] (SPEC-E4 §4).

Stage 1 of the ingestion pipeline. Turns each ``EvidenceItem`` into one or more
``Proposal`` objects without touching any committed file. The deterministic core
maps a ``RelationSchema`` directly to a ``semantics/<conn>/<name>.yaml`` draft and is
the only builder path in headless mode (SPEC-E4 §9). LLM-assisted drafting (grain
without a primary key, joins from observed queries) is a parallel sub-track gated
behind an injected ``LLMDrafter``; the default is a deterministic null stub.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from canon.connectors.base import (
    ForeignKey,
    RelationSchema,
)
from canon.ingestion.models import (
    DraftedBy,
    EvidenceItem,
    EvidenceKind,
    Proposal,
    ProposalOp,
)
from canon.semantic.models import Provenance, Relationship

__all__ = [
    "BuildResult",
    "ContextBuilder",
    "GrainDraft",
    "LLMDrafter",
    "NullLLMDrafter",
    "SkippedEvidence",
]

# Confidence the builder assigns to its two drafting origins (SPEC-E4 §4): a
# deterministic inference is fully trusted; an LLM-drafted grain is a labelled
# candidate carrying lower certainty so it sorts behind deterministic facts in review.
DETERMINISTIC_CONFIDENCE = 1.0
LLM_GRAIN_CONFIDENCE = 0.3


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


class LLMDrafter(Protocol):
    """The fuzzy-drafting seam (SPEC-E4 §4) — injected so the headless path stays LLM-free.

    Implementations propose the parts the deterministic core cannot assert: a grain when
    no primary key is declared, and joins inferred from observed-query evidence. Every
    result is labelled ``drafted_by: llm`` by the builder and carries reduced confidence.
    """

    def draft_grain(self, schema: RelationSchema) -> GrainDraft:
        """Propose a grain for a relation that declares no primary key."""
        ...

    def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
        """Propose joins from an ``observed_query`` payload."""
        ...


class NullLLMDrafter:
    """Default LLM stub for the headless path — proposes nothing, asserts nothing.

    Returns an empty grain candidate (so the proposal labels grain as a draft rather than
    silently asserting one, SPEC-E4 §4 / S1-AC2) and no joins. A real LLM drafter (E10)
    is injected to replace it in interactive mode.
    """

    def draft_grain(self, schema: RelationSchema) -> GrainDraft:  # noqa: ARG002 — stub
        return GrainDraft(grain=[], confidence=LLM_GRAIN_CONFIDENCE)

    def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
        return []


class ContextBuilder:
    """Builds ``Proposal[]`` from normalized evidence (SPEC-E4 §4).

    Stateless and free of file I/O: ``build`` is a pure function of its evidence input
    (and the injected drafter). With the default ``NullLLMDrafter`` the run is fully
    deterministic — identical evidence yields identical proposals (SPEC-E4 §9, S9-AC1).
    """

    def __init__(self, llm_drafter: LLMDrafter | None = None) -> None:
        self._llm_drafter: LLMDrafter = llm_drafter or NullLLMDrafter()

    def build(self, evidence: list[EvidenceItem]) -> BuildResult:
        """Turn evidence into proposals, recording anything it cannot handle.

        Iterates in input order for determinism. Unknown kinds — and known kinds without
        a handler yet — are recorded in ``skipped`` and never raise (SPEC-E4 §3, S1-AC4).
        """
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
                proposals.append(self._build_relation_schema(item))
            else:
                skipped.append(
                    SkippedEvidence(
                        source=item.source,
                        kind=item.kind,
                        reason="no handler yet (deferred to a later E4 stage)",
                    )
                )

        return BuildResult(proposals=proposals, skipped=skipped)

    def _build_relation_schema(self, item: EvidenceItem) -> Proposal:
        """Map one ``RelationSchema`` to a ``semantics/<conn>/<name>.yaml`` draft proposal.

        With a declared primary key the grain is asserted deterministically with full
        confidence. Without one, the grain is an LLM-drafted candidate: labelled
        ``drafted_by: llm``, carrying reduced confidence, with the grain marked as a draft
        in ``meta`` rather than silently asserted (SPEC-E4 §4, S1-AC2).
        """
        schema = RelationSchema.model_validate(item.payload)
        name = schema.relation.split(".")[-1]

        meta: dict[str, Any] = {"source_fingerprint": schema.source_fingerprint}

        if schema.primary_key:
            grain = list(schema.primary_key)
            drafted_by = DraftedBy.DETERMINISTIC
            confidence = DETERMINISTIC_CONFIDENCE
        else:
            draft = self._llm_drafter.draft_grain(schema)
            grain = list(draft.grain)
            meta["grain_draft"] = True
            drafted_by = DraftedBy.LLM
            confidence = draft.confidence

        content: dict[str, Any] = {
            "name": name,
            "connection": schema.connection,
            "table": schema.relation,
            "grain": grain,
            "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable} for c in schema.columns
            ],
            "joins": [self._fk_to_join(name, fk) for fk in schema.foreign_keys],
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
        )

    @staticmethod
    def _fk_to_join(this_name: str, fk: ForeignKey) -> dict[str, Any]:
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
        return {"to": to, "on": on, "relationship": Relationship.MANY_TO_ONE.value}
