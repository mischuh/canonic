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
from canon.connectors.base import AcquisitionTier
from canon.contracts.loader import load_metric_bindings
from canon.ingestion.builder import _DA_SENTINEL
from canon.ingestion.models import (
    DraftedBy,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.semantic.loader import list_semantic_sources
from canon.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "AcceptedStore",
    "DiskAcceptedStore",
    "ExistingFact",
    "InMemoryAcceptedStore",
    "NullReconcileDrafter",
    "ReconcileDrafter",
    "ReconciliationEngine",
    "ResolutionDraft",
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


# Curation rank within the inferred provenance band (SPEC-E3 §7, S6).
# MODELING (dbt/LookML) is better-curated than raw introspection; all other tiers rank
# equally so any disagreement among them still surfaces as a contradiction (S4).
_CURATION_RANK: dict[AcquisitionTier, int] = {
    AcquisitionTier.LIVE: 0,
    AcquisitionTier.QUERY_HISTORY: 0,
    AcquisitionTier.DECLARATIVE: 0,
    AcquisitionTier.SAMPLE: 0,
    AcquisitionTier.HAND_AUTHORED: 0,
    AcquisitionTier.MODELING: 1,
}


def _curation_rank(tier: AcquisitionTier) -> int:
    """Numeric curation rank for an acquisition tier within the inferred band (SPEC-E3 §7)."""
    return _CURATION_RANK.get(tier, 0)


def _column_types(content: dict[str, Any]) -> dict[str, str]:
    """Extract column-name → normalized-type from a proposal's content dict."""
    cols = content.get("columns")
    if not isinstance(cols, list):
        return {}
    return {
        c["name"]: c["type"] for c in cols if isinstance(c, dict) and "name" in c and "type" in c
    }


def _has_type_conflict(winner: Proposal, others: list[Proposal]) -> bool:
    """True if any lower-tier proposal disagrees on a column type with the winner.

    Missing or extra columns are not type conflicts — only same-name/different-type
    divergence triggers a contradiction (mirrors acquisition.TypeConflict semantics).
    """
    winner_types = _column_types(winner.content)
    if not winner_types:
        return False
    for other in others:
        other_types = _column_types(other.content)
        for col, typ in other_types.items():
            if col in winner_types and winner_types[col] != typ:
                return True
    return False


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


class DiskAcceptedStore:
    """:class:`AcceptedStore` backed by committed ``semantics/*.yaml`` and contract files.

    Loads every accepted semantic source and every ``contracts/metrics/*.yaml``
    :class:`~canon.contracts.models.MetricBinding` under ``project_root`` once (eagerly,
    so a re-run over an unchanged tree yields identical facts) and projects each onto an
    :class:`ExistingFact`.  Contract files are loaded with ``provenance=human_curated``
    (the default binding provenance) so the tier check in ``_reconcile_one`` reflects the
    correct authority — a deprecated-alternative EDIT proposal never outranks the binding.
    """

    def __init__(self, project_root: Path) -> None:
        self._facts: dict[str, ExistingFact] = {}

        for source in list_semantic_sources(project_root):
            target = f"semantics/{source.connection}/{source.name}.yaml"
            self._facts[target] = ExistingFact(
                target=target,
                content=source.model_dump(mode="json"),
                provenance=source.meta.provenance,
                frozen=source.meta.frozen,
                source_fingerprint=source.meta.source_fingerprint,
            )

        for binding in load_metric_bindings(project_root):
            slug = binding.metric.replace(" ", "_").lower()
            target = f"contracts/metrics/{slug}.yaml"
            self._facts[target] = ExistingFact(
                target=target,
                content=binding.model_dump(mode="json"),
                provenance=binding.provenance,
                frozen=False,
                source_fingerprint=None,
            )

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


class ResolutionDraft(BaseModel):
    """An LLM-drafted resolution selecting one side of a contradiction (SPEC-E10 §3)."""

    model_config = ConfigDict(frozen=True)

    winner_index: int
    confidence: float = 0.5


class ReconcileDrafter(Protocol):
    """The conflict-resolution seam (SPEC-E4 §5.4) — injected so the headless path stays LLM-free.

    Implementations select one proposal from a set of contradicting candidates using the stronger
    model configured for the ``reconcile`` task (SPEC-E10 §3). Returns ``None`` to defer to the
    human reviewer (no silent winner). The headless default is :class:`NullReconcileDrafter`.
    """

    async def draft_resolution(
        self, target: str, proposals: list[Proposal]
    ) -> ResolutionDraft | None:
        """Propose the winning proposal index for a contradicting group at ``target``."""
        ...


class NullReconcileDrafter:
    """Default reconcile stub for the headless path — resolves nothing, asserts nothing.

    Returns ``None`` so every contradiction is kept for human review. A real drafter (E10)
    is injected to replace it in interactive mode.
    """

    async def draft_resolution(
        self,
        target: str,
        proposals: list[Proposal],  # noqa: ARG002 — stub
    ) -> ResolutionDraft | None:
        return None


class ReconciliationEngine:
    """Applies the SPEC-E4 §5 decision table to proposals against accepted files.

    Stateless and free of file I/O: ``reconcile`` is a pure function of its proposals, the
    injected accepted store, and the policy, so identical inputs yield an identical report
    (headless determinism, §9 / S9-AC1). Contradictions are surfaced, never raised: a run
    never fails on them by default (§5.4).

    The optional ``drafter`` is used only by :meth:`refine` — the async post-pass that lets
    the interactive path LLM-resolve intra-run contradictions after the deterministic pass.
    """

    def __init__(
        self,
        config: ReconcileConfig | None = None,
        drafter: ReconcileDrafter | None = None,
    ) -> None:
        self._config: ReconcileConfig = config or ReconcileConfig()
        self._drafter: ReconcileDrafter = drafter or NullReconcileDrafter()

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
                entries.extend(self._resolve_group(target, group, accepted))
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

    async def refine(
        self, report: ReconciliationReport, accepted: AcceptedStore
    ) -> ReconciliationReport:
        """LLM-resolve intra-run contradictions using the injected drafter (SPEC-E10 §3).

        Groups CONTRADICTION entries by target. Targets with ≥2 entries are intra-run
        contradictions (multiple sources disagreed in the same run) and are eligible for
        LLM resolution. Targets with exactly 1 entry are policy or provenance contradictions
        (frozen fact, higher-tier existing) and are always left unchanged.

        When the drafter selects a valid winner, the group is replaced by the deterministic
        decision for that proposal via ``_reconcile_one``. On ``None`` or an out-of-range
        index the original CONTRADICTION entries are kept as-is. ``NullReconcileDrafter``
        is a transparent no-op.
        """
        contradiction_groups: dict[str, list[ReconciliationEntry]] = {}
        for entry in report.entries:
            if entry.decision is ReconciliationDecision.CONTRADICTION:
                contradiction_groups.setdefault(entry.target, []).append(entry)

        replacements: dict[str, list[ReconciliationEntry]] = {}
        for target, group in contradiction_groups.items():
            if len(group) < 2:
                continue
            proposals = [e.proposal for e in group]
            draft = await self._drafter.draft_resolution(target, proposals)
            if draft is None or not (0 <= draft.winner_index < len(proposals)):
                continue
            winning = proposals[draft.winner_index]
            replacements[target] = [self._reconcile_one(winning, accepted.get(target))]

        if not replacements:
            return report

        new_entries: list[ReconciliationEntry] = []
        emitted: set[str] = set()
        for entry in report.entries:
            if entry.decision is not ReconciliationDecision.CONTRADICTION:
                new_entries.append(entry)
            elif entry.target in replacements:
                if entry.target not in emitted:
                    new_entries.extend(replacements[entry.target])
                    emitted.add(entry.target)
            else:
                new_entries.append(entry)

        return ReconciliationReport(entries=new_entries)

    @staticmethod
    def _group_by_target(proposals: list[Proposal]) -> dict[str, list[Proposal]]:
        groups: dict[str, list[Proposal]] = {}
        for proposal in proposals:
            groups.setdefault(proposal.target, []).append(proposal)
        return groups

    def _resolve_group(
        self, target: str, group: list[Proposal], accepted: AcceptedStore
    ) -> list[ReconciliationEntry]:
        """Resolve a disagreeing group using tier preference (SPEC-E3 §7, S6).

        If a single modeling-tier proposal unambiguously wins over all lower-tier ones
        and there is no column-type conflict, it is passed to the normal decision table.
        Any genuine type conflict, or a tie at the top rank, falls back to contradiction
        so neither side silently wins (S4).

        Provenance boundary: the winning proposal still carries ``provenance=INFERRED``
        and runs through ``_reconcile_one``, where the existing tier check (§5.1) prevents
        it from overwriting any ``human_curated`` or ``board_approved`` fact.
        """
        max_rank = max(_curation_rank(p.acquisition_tier) for p in group)
        winners = [p for p in group if _curation_rank(p.acquisition_tier) == max_rank]
        losers = [p for p in group if _curation_rank(p.acquisition_tier) < max_rank]

        if losers and len({_signature(p) for p in winners}) == 1:
            winner = winners[0]
            if not _has_type_conflict(winner, losers):
                return [self._reconcile_one(winner, accepted.get(target))]
            action = f"modeling and introspection disagree on column types for {target}; resolve manually"
        else:
            action = f"multiple sources disagree on {target}; resolve manually"

        return self._contradicting_sources(target, group, action)

    @staticmethod
    def _contradicting_sources(
        target: str, group: list[Proposal], action: str | None = None
    ) -> list[ReconciliationEntry]:
        """Flag every side of an intra-run disagreement so none silently wins (§5.4 / S4)."""
        if action is None:
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

        # Deprecated-alternative additive merge (SPEC-E3 §3.3, FR-13).
        # When the proposal carries the DA sentinel the engine merges the entry into the
        # existing MetricBinding's deprecated_alternatives list rather than applying the
        # normal tier rules — the canonical binding is never touched.
        if _DA_SENTINEL in proposal.content:
            return self._merge_deprecated_alternative(proposal, existing)

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

    def _merge_deprecated_alternative(
        self, proposal: Proposal, existing: ExistingFact
    ) -> ReconciliationEntry:
        """Additively merge a deprecated-alternative entry into an existing MetricBinding.

        Reads the existing binding's ``deprecated_alternatives`` list and appends the
        incoming entry if it is not already present (idempotent on ``ref``).  The
        resulting full binding content becomes the merged proposal the emitter renders.
        If the entry already exists the decision is ``no_op``.

        The ``canonical`` field is never modified — FR-13 invariant at the reconciliation
        layer.  Frozen bindings are respected: a frozen binding never receives additive
        updates from inferred evidence.
        """
        if existing.frozen:
            return ReconciliationEntry(
                decision=ReconciliationDecision.CONTRADICTION,
                target=proposal.target,
                proposal=proposal,
                existing=existing.content,
                existing_provenance=existing.provenance,
                existing_frozen=True,
                recommended_action=(
                    "binding is frozen; deprecated-alternative evidence recorded but not merged"
                ),
            )

        da_fragment: dict[str, Any] = dict(proposal.content[_DA_SENTINEL])
        incoming_ref: str = str(da_fragment.get("ref", ""))

        existing_das: list[dict[str, Any]] = list(
            existing.content.get("deprecated_alternatives", []) or []
        )
        if any(str(da.get("ref", "")) == incoming_ref for da in existing_das):
            # Already recorded — idempotent no-op.
            return ReconciliationEntry(
                decision=ReconciliationDecision.NO_OP,
                target=proposal.target,
                proposal=proposal,
                existing=existing.content,
                existing_provenance=existing.provenance,
            )

        merged_das = existing_das + [da_fragment]
        merged_content: dict[str, Any] = dict(existing.content)
        merged_content["deprecated_alternatives"] = merged_das

        merged_proposal = proposal.model_copy(
            update={"content": merged_content, "op": ProposalOp.EDIT}
        )
        return ReconciliationEntry(
            decision=ReconciliationDecision.EDIT,
            target=proposal.target,
            proposal=merged_proposal,
            existing=existing.content,
            existing_provenance=existing.provenance,
            auto_apply=self._auto_apply_eligible(
                merged_proposal, existing, ReconciliationDecision.EDIT
            ),
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
