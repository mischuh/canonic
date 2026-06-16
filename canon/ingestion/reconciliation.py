"""Reconciliation engine — Proposal[] × accepted files → ReconciliationReport (SPEC-E4 §5).

Stage 2 of the ingestion pipeline. Merges each :class:`Proposal` from the builder against
the currently-accepted context file under deterministic provenance rules. The decision is a
pure function of ``(existing fact, proposal, freeze state, policy)`` — the upstream drafting
may have used an LLM, but the *decision* never does (SPEC-E4 §5, §9). Nothing here writes a
file: the engine emits a report of decisions; applying them is a separate, reviewed step.

The accepted state is supplied through the injected :class:`AcceptedStore` protocol, mirroring
the builder's injected ``LLMDrafter`` so the engine stays stateless and free of file I/O.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict

from canon.config import ReconcileConfig
from canon.ingestion.models import (
    DraftedBy,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "AcceptedStore",
    "ExistingFact",
    "InMemoryAcceptedStore",
    "ReconciliationEngine",
]

# Provenance authority ordering (SPEC-E4 §5.1): board_approved > human_curated > inferred.
# Higher always wins; ingest enters at INFERRED and can never displace a higher tier.
_TIER_ORDER: dict[Provenance, int] = {
    Provenance.INFERRED: 0,
    Provenance.HUMAN_CURATED: 1,
    Provenance.BOARD_APPROVED: 2,
}


def _tier(provenance: Provenance) -> int:
    """Numeric authority rank for a provenance tier (SPEC-E4 §5.1)."""
    return _TIER_ORDER[provenance]


class ExistingFact(BaseModel):
    """The engine's view of one currently-accepted context file (SPEC-E4 §5).

    A loader maps a committed ``semantics/*.yaml`` (its ``meta.provenance``,
    ``meta.frozen``, ``meta.source_fingerprint``) onto this shape; the engine never reads
    files itself. ``frozen`` lives here rather than in the E5 file schema, which the spec
    leaves as an open touchpoint (§11) — the engine only consumes the resolved bool.
    """

    model_config = ConfigDict(frozen=True)

    target: str
    content: dict[str, Any]
    provenance: Provenance = Provenance.INFERRED
    frozen: bool = False
    source_fingerprint: str | None = None


class AcceptedStore(Protocol):
    """Read-only lookup over the currently-accepted files (SPEC-E4 §5).

    Injected so the engine stays pure: an in-memory store backs tests and the headless
    path, while a disk-backed adapter (out of scope here) supplies real projects.
    """

    def get(self, target: str) -> ExistingFact | None:
        """Return the accepted fact at ``target``, or ``None`` if none exists yet."""
        ...

    def targets(self) -> Iterable[str]:
        """All accepted targets — used to detect disappeared evidence (§5.2 / S10)."""
        ...


class InMemoryAcceptedStore:
    """Dict-backed :class:`AcceptedStore` for tests and the headless path."""

    def __init__(self, facts: Iterable[ExistingFact] = ()) -> None:
        self._facts: dict[str, ExistingFact] = {f.target: f for f in facts}

    def get(self, target: str) -> ExistingFact | None:
        return self._facts.get(target)

    def targets(self) -> Iterable[str]:
        return tuple(self._facts)


def _proposal_fingerprint(proposal: Proposal) -> str | None:
    """The source fingerprint a proposal carries, for idempotency/equality (SPEC-E4 §7).

    Prefers the fingerprint embedded in ``content.meta`` (where the builder writes it),
    falling back to the first anchored evidence fingerprint.
    """
    meta = proposal.content.get("meta")
    if isinstance(meta, dict) and meta.get("source_fingerprint"):
        return str(meta["source_fingerprint"])
    return proposal.anchored_to[0] if proposal.anchored_to else None


def _signature(proposal: Proposal) -> str:
    """A stable distinctness key for grouping proposals at one target (SPEC-E4 §5.4).

    Two proposals are "the same" when they share a fingerprint; lacking one, their
    serialized content decides, so genuinely disagreeing sources are detected.
    """
    fp = _proposal_fingerprint(proposal)
    if fp is not None:
        return f"fp:{fp}"
    return "content:" + json.dumps(proposal.content, sort_keys=True, default=str)


class ReconciliationEngine:
    """Applies the SPEC-E4 §5 decision table to proposals against accepted files.

    Stateless and free of file I/O: ``reconcile`` is a pure function of its proposals, the
    injected accepted store, and the policy, so identical inputs yield an identical report
    (headless determinism, §9 / S9-AC1). Contradictions are surfaced, never raised: a run
    never fails on them by default (§5.4).
    """

    def __init__(self, config: ReconcileConfig | None = None) -> None:
        self._config: ReconcileConfig = config or ReconcileConfig()

    def reconcile(self, proposals: list[Proposal], accepted: AcceptedStore) -> ReconciliationReport:
        """Reconcile ``proposals`` against ``accepted`` and return the decision report.

        Proposals are grouped by target in first-seen order (deterministic). A target with
        proposals that disagree is flagged as an intra-run contradiction with every side
        recorded (§5.4 / S4); otherwise the §5.2 decision table is applied against the
        accepted fact. Finally, accepted ``inferred`` facts that no proposal addressed are
        proposed for prune (§5.2 last row / S10).
        """
        entries: list[ReconciliationEntry] = []
        groups = self._group_by_target(proposals)

        for target, group in groups.items():
            if len({_signature(p) for p in group}) > 1:
                entries.extend(self._contradicting_sources(target, group))
            else:
                entries.append(self._reconcile_one(group[0], accepted.get(target)))

        entries.extend(self._prune_disappeared(set(groups), accepted))
        return ReconciliationReport(entries=entries)

    def contradictions_block(self, report: ReconciliationReport) -> bool:
        """Whether strict mode should gate the run on this report's contradictions (§5.4).

        The engine never fails a run itself; the caller (CLI/CI) maps this to an exit code.
        """
        contradictions = report.summary[ReconciliationDecision.CONTRADICTION.value]
        return self._config.strict_contradictions and contradictions > 0

    @staticmethod
    def _group_by_target(proposals: list[Proposal]) -> dict[str, list[Proposal]]:
        groups: dict[str, list[Proposal]] = {}
        for proposal in proposals:
            groups.setdefault(proposal.target, []).append(proposal)
        return groups

    @staticmethod
    def _contradicting_sources(target: str, group: list[Proposal]) -> list[ReconciliationEntry]:
        """Flag every side of an intra-run disagreement so none silently wins (§5.4 / S4)."""
        action = f"multiple sources disagree on {target}; resolve manually"
        return [
            ReconciliationEntry(
                decision=ReconciliationDecision.CONTRADICTION,
                target=target,
                proposal=proposal,
                recommended_action=action,
            )
            for proposal in group
        ]

    def _reconcile_one(
        self, proposal: Proposal, existing: ExistingFact | None
    ) -> ReconciliationEntry:
        """Apply the §5.2 decision table to one proposal against its accepted fact."""
        if existing is None:
            return ReconciliationEntry(
                decision=ReconciliationDecision.ADD,
                target=proposal.target,
                proposal=proposal,
                auto_apply=self._auto_apply_eligible(proposal, None, ReconciliationDecision.ADD),
            )

        def entry(
            decision: ReconciliationDecision,
            *,
            recommended_action: str | None = None,
            auto_apply: bool = False,
            low_confidence: bool = False,
            existing_frozen: bool = False,
        ) -> ReconciliationEntry:
            return ReconciliationEntry(
                decision=decision,
                target=proposal.target,
                proposal=proposal,
                existing=existing.content,
                existing_provenance=existing.provenance,
                recommended_action=recommended_action,
                auto_apply=auto_apply,
                low_confidence=low_confidence,
                existing_frozen=existing_frozen,
            )

        if self._fingerprints_match(existing, proposal):
            return entry(ReconciliationDecision.NO_OP)

        # Conflict: a higher-tier or frozen fact is flagged, never edited.
        if existing.frozen:
            return entry(
                ReconciliationDecision.CONTRADICTION,
                existing_frozen=True,
                recommended_action=(
                    "fact is frozen; conflicting evidence flagged — unfreeze and re-ingest "
                    "to accept the change"
                ),
            )
        if _tier(existing.provenance) > _tier(proposal.provenance):
            return entry(
                ReconciliationDecision.CONTRADICTION,
                recommended_action=(
                    f"existing {existing.provenance.value} fact outranks the inferred "
                    "proposal; resolve manually"
                ),
            )

        # Existing tier <= proposal tier: propose an edit, marking low confidence for review.
        return entry(
            ReconciliationDecision.EDIT,
            low_confidence=proposal.confidence < self._config.auto_apply.min_confidence,
            auto_apply=self._auto_apply_eligible(proposal, existing, ReconciliationDecision.EDIT),
        )

    def _prune_disappeared(
        self, proposed_targets: set[str], accepted: AcceptedStore
    ) -> list[ReconciliationEntry]:
        """Propose prune for accepted inferred facts no proposal addressed (§5.2 / S10).

        Only ``inferred``, non-frozen facts are pruned; higher-tier or frozen facts whose
        evidence disappeared are left untouched rather than silently removed.
        """
        entries: list[ReconciliationEntry] = []
        for target in sorted(accepted.targets()):
            if target in proposed_targets:
                continue
            fact = accepted.get(target)
            if fact is None or fact.frozen or fact.provenance is not Provenance.INFERRED:
                continue
            entries.append(
                ReconciliationEntry(
                    decision=ReconciliationDecision.PRUNE,
                    target=target,
                    proposal=Proposal(
                        target=target,
                        op=ProposalOp.PRUNE,
                        content={},
                        provenance=Provenance.INFERRED,
                        confidence=1.0,
                        drafted_by=DraftedBy.DETERMINISTIC,
                    ),
                    existing=fact.content,
                    existing_provenance=fact.provenance,
                    recommended_action="source evidence disappeared; prune or mark stale",
                )
            )
        return entries

    @staticmethod
    def _fingerprints_match(existing: ExistingFact, proposal: Proposal) -> bool:
        """True iff the accepted fact and proposal share a known source fingerprint (§5.2)."""
        fp = _proposal_fingerprint(proposal)
        return fp is not None and fp == existing.source_fingerprint

    def _auto_apply_eligible(
        self,
        proposal: Proposal,
        existing: ExistingFact | None,
        decision: ReconciliationDecision,
    ) -> bool:
        """Whether the auto-apply policy permits applying this entry unreviewed (§5.5).

        Opt-in only, bounded by confidence, capped at ``max_provenance``, and refused for
        any proposal that touches a structural ``never`` field — so a ``grain`` change is
        always proposed for review regardless of confidence (S5-AC2).
        """
        policy = self._config.auto_apply
        if not policy.enabled:
            return False
        if decision not in (ReconciliationDecision.ADD, ReconciliationDecision.EDIT):
            return False
        if proposal.confidence < policy.min_confidence:
            return False
        if _tier(proposal.provenance) > _tier(policy.max_provenance):
            return False
        return not self._touches_never_field(proposal, existing, policy.never)

    @staticmethod
    def _touches_never_field(
        proposal: Proposal, existing: ExistingFact | None, never: list[str]
    ) -> bool:
        """True if the proposal sets or changes a structural field on the denylist (§5.5)."""
        for field in never:
            new_value = proposal.content.get(field)
            if existing is None:
                if new_value not in (None, [], {}, ""):
                    return True
            elif existing.content.get(field) != new_value:
                return True
        return False
