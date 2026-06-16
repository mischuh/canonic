"""E4 ingestion & reconciliation engine (SPEC-E4)."""

from canon.ingestion.builder import (
    BuildResult,
    ContextBuilder,
    GrainDraft,
    LLMDrafter,
    NullLLMDrafter,
    SkippedEvidence,
)
from canon.ingestion.models import (
    KNOWN_EVIDENCE_KINDS,
    DraftedBy,
    EvidenceItem,
    EvidenceKind,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)

__all__ = [
    "BuildResult",
    "ContextBuilder",
    "DraftedBy",
    "EvidenceItem",
    "EvidenceKind",
    "GrainDraft",
    "KNOWN_EVIDENCE_KINDS",
    "LLMDrafter",
    "NullLLMDrafter",
    "Proposal",
    "ProposalOp",
    "ReconciliationDecision",
    "ReconciliationEntry",
    "ReconciliationReport",
    "SkippedEvidence",
]
