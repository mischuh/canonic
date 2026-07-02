"""Ingestion pipeline data models — evidence contract, proposals, reconciliation (SPEC-E4 §3–§5).

These types are the transport-neutral boundary that keeps every source vendor out of the
engine.  The builder, reconciliation engine, and diff emitter all operate on these; no
vendor-specific shape crosses into any downstream stage.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from canonic.connectors.base import (
    AcquisitionTier,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.semantic.models import (
    Provenance,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)

__all__ = [
    "DraftedBy",
    "EvidenceItem",
    "EvidenceKind",
    "KNOWN_EVIDENCE_KINDS",
    "Proposal",
    "ProposalOp",
    "ReconciliationDecision",
    "ReconciliationEntry",
    "ReconciliationReport",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EvidenceKind(StrEnum):
    """Known evidence kinds dispatched by the builder (SPEC-E4 §3).

    ``EvidenceItem.kind`` is a plain ``str`` (open set) so future kinds — e.g.
    ``answer_outcome`` from E11 — are accepted without code changes.  This enum
    provides the known-kind constants used for dispatch and the ``KNOWN_EVIDENCE_KINDS``
    membership set.
    """

    RELATION_SCHEMA = "relation_schema"
    OBSERVED_QUERY = "observed_query"
    DEFINITION = "definition"
    DOC_EVIDENCE = "doc_evidence"
    USAGE_EVIDENCE = "usage_evidence"


class DraftedBy(StrEnum):
    """Whether a proposal was produced deterministically or by an LLM (SPEC-E4 §4)."""

    DETERMINISTIC = "deterministic"
    LLM = "llm"


class ProposalOp(StrEnum):
    """The operation a proposal requests against the target file (SPEC-E4 §4)."""

    ADD = "add"
    EDIT = "edit"
    PRUNE = "prune"


class ReconciliationDecision(StrEnum):
    """The outcome the reconciliation engine assigned to a proposal (SPEC-E4 §5.2)."""

    ADD = "add"
    CONTRADICTION = "contradiction"
    EDIT = "edit"
    NO_OP = "no_op"
    PRUNE = "prune"


# ---------------------------------------------------------------------------
# Module-level constant — membership test for the known-kind dispatch set
# ---------------------------------------------------------------------------

KNOWN_EVIDENCE_KINDS: frozenset[str] = frozenset(EvidenceKind)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    """A normalized, self-describing evidence record (SPEC-E4 §3).

    ``kind`` is an open ``str`` field so unknown kinds (e.g. from E11 or future
    connectors) are accepted without raising.  Use ``is_known()`` to decide
    whether the builder has a registered handler.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    kind: str
    acquisition_tier: AcquisitionTier
    payload: dict[str, Any]
    source_fingerprint: str
    observed_at: datetime

    def is_known(self) -> bool:
        """Return True iff ``kind`` is in the registered dispatch set."""
        return self.kind in KNOWN_EVIDENCE_KINDS


class Proposal(BaseModel):
    """A proposed add/edit/prune against a context file — not a file write (SPEC-E4 §4).

    ``provenance`` defaults to ``INFERRED`` because all new evidence enters at the
    lowest tier; the reconciliation engine enforces that it can never overwrite a
    higher tier (§5.1).  ``confidence`` is bounded [0, 1] and drives the
    propose-vs-auto-apply decision (§5.5).  ``acquisition_tier`` carries the curation
    rank of the originating evidence so the engine can prefer modeling-tier evidence
    over raw introspection within the inferred provenance band (SPEC-E3 §7, S6).
    """

    model_config = ConfigDict(frozen=True)

    target: str
    op: ProposalOp
    content: dict[str, Any]
    provenance: Provenance = Provenance.INFERRED
    confidence: float = Field(ge=0.0, le=1.0)
    anchored_to: list[str] = []
    drafted_by: DraftedBy = DraftedBy.DETERMINISTIC
    acquisition_tier: AcquisitionTier = AcquisitionTier.LIVE


class ReconciliationEntry(BaseModel):
    """One reconciliation decision with both sides recorded (SPEC-E4 §5.2, §5.4).

    ``auto_apply`` records whether the auto-apply policy (§5.5) deems this entry eligible
    to be applied without review; it is ``False`` under the default propose-only config.
    ``low_confidence`` flags an ``EDIT`` whose confidence fell below the policy threshold
    (decision-table row 6) so a reviewer sorts it accordingly. ``existing_frozen`` records
    that a flagged contradiction was driven by a frozen fact (§5.3).
    """

    model_config = ConfigDict(frozen=True)

    decision: ReconciliationDecision
    target: str
    proposal: Proposal
    existing: dict[str, Any] | None = None
    existing_provenance: Provenance | None = None
    recommended_action: str | None = None
    auto_apply: bool = False
    low_confidence: bool = False
    existing_frozen: bool = False


class ReconciliationReport(BaseModel):
    """Audit-friendly result of a reconciliation run (SPEC-E4 §5.4, §6)."""

    model_config = ConfigDict(frozen=True)

    entries: list[ReconciliationEntry] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> dict[str, int]:
        """Decision counts — present in model_dump() for CI/JSON output."""
        counts: dict[str, int] = {d.value: 0 for d in ReconciliationDecision}
        for entry in self.entries:
            counts[entry.decision.value] += 1
        return counts
