"""E4 ingestion & reconciliation engine (SPEC-E4)."""

from canonic.ingestion.builder import (
    BuildResult,
    ContextBuilder,
    GrainDraft,
    LLMDrafter,
    NullLLMDrafter,
    SkippedEvidence,
)
from canonic.ingestion.emitter import (
    AuditTrailWriter,
    ContradictionNote,
    DiffEmitter,
    DiffFormat,
    DiskEventLog,
    DiskSnapshotStore,
    EmissionResult,
    EmittedDiff,
    EventLog,
    SnapshotStore,
)
from canonic.ingestion.models import (
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
from canonic.ingestion.pipeline import IngestionPipeline, PipelineResult
from canonic.ingestion.reconciliation import (
    AcceptedStore,
    DiskAcceptedStore,
    ExistingFact,
    InMemoryAcceptedStore,
    ReconciliationEngine,
)
from canonic.ingestion.source import evidence_from_introspection, gather_evidence
from canonic.ingestion.validation import (
    ValidationGate,
    ValidationReport,
    Violation,
    ViolationKind,
)

__all__ = [
    "AcceptedStore",
    "AuditTrailWriter",
    "BuildResult",
    "ContextBuilder",
    "ContradictionNote",
    "DiffEmitter",
    "DiffFormat",
    "DiskAcceptedStore",
    "DiskEventLog",
    "DiskSnapshotStore",
    "DraftedBy",
    "EmissionResult",
    "EmittedDiff",
    "EventLog",
    "EvidenceItem",
    "EvidenceKind",
    "ExistingFact",
    "GrainDraft",
    "IngestionPipeline",
    "InMemoryAcceptedStore",
    "KNOWN_EVIDENCE_KINDS",
    "LLMDrafter",
    "NullLLMDrafter",
    "PipelineResult",
    "Proposal",
    "ProposalOp",
    "ReconciliationDecision",
    "ReconciliationEngine",
    "ReconciliationEntry",
    "ReconciliationReport",
    "SkippedEvidence",
    "SnapshotStore",
    "ValidationGate",
    "ValidationReport",
    "Violation",
    "ViolationKind",
    "evidence_from_introspection",
    "gather_evidence",
]
