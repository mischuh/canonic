"""Ingestion pipeline — the four E4 stages wired into one runnable flow (SPEC-E4 §2).

Orchestrates builder → reconciliation → validation → diff emission (SPEC-E4 §2), threading the
audit trail (§6), fingerprint idempotency (§7), and the fast initial bootstrap (§8) around them.
Each stage stays the pure, independently-tested component it already is; this module only
composes them and owns the run's side effects.

Determinism (SPEC-E4 §9): with the default ``NullLLMDrafter`` the builder is LLM-free, the
reconciliation decision is a pure function of (evidence, accepted state, policy), and emission is
pure — so identical inputs yield byte-identical proposals and decisions. The only in-place write
on an unchanged run is the ``last_validated_at`` freshness stamp on no-op targets, explicitly
carved out by §5.2/§7; it touches no proposal and no decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from canon.ingestion.builder import ContextBuilder, LLMDrafter, NullLLMDrafter, SkippedEvidence
from canon.ingestion.emitter import AuditTrailWriter, DiffEmitter, EmissionResult, EmittedDiff
from canon.ingestion.models import ProposalOp, ReconciliationDecision, ReconciliationReport
from canon.ingestion.reconciliation import DiskAcceptedStore, ReconciliationEngine
from canon.ingestion.validation import ValidationGate
from canon.semantic.loader import dump_semantic_source, load_semantic_source

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from canon.config import ReconcileConfig
    from canon.connectors.base import ConnectorBase
    from canon.ingestion.models import EvidenceItem

__all__ = ["IngestionPipeline", "PipelineResult", "write_emitted_diffs"]


def write_emitted_diffs(project_root: Path, diffs: Iterable[EmittedDiff]) -> None:
    """Materialize emitted diffs by writing each ``after`` state to its target file.

    The one place that turns an :class:`EmittedDiff` into an on-disk change: ``PRUNE`` unlinks
    the target, every other op writes the exact ``after`` content the validation gate already
    accepted. Shared by the pipeline (bootstrap writes / bounded auto-apply, §5.5) and the
    headless auto-PR step (SPEC-E4 §6), so both apply diffs identically.
    """
    for diff in diffs:
        path = project_root / diff.target
        if diff.op is ProposalOp.PRUNE:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(diff.after or "")


class PipelineResult(BaseModel):
    """The outcome of one ingest run: the reviewable emission plus the builder's skip ledger.

    Wraps :class:`EmissionResult` (which already carries the ``ReconciliationReport``, the
    emitted diffs, and the contradiction notes, and serializes via ``to_json`` /
    ``render_markdown``) so the CLI has a single object to render. ``skipped`` records evidence
    the builder could not handle (SPEC-E4 §3) — never an error.
    """

    model_config = ConfigDict(frozen=True)

    emission: EmissionResult
    skipped: list[SkippedEvidence] = []

    @property
    def report(self) -> ReconciliationReport:
        """The reconciliation report (SPEC-E4 §5.4) — the issue's nominal ``run`` return."""
        return self.emission.report


class IngestionPipeline:
    """Composes the four E4 stages into one ingest run (SPEC-E4 §2).

    Stateless across runs: ``run`` and ``bootstrap`` take the evidence/connection and read the
    accepted state fresh from disk each time, so a re-run over an unchanged tree reconciles
    against the same facts and proposes nothing (idempotency, §7). The injected ``connectors``
    map backs the tier 4–6 validation probe (§10); the default builder is deterministic.
    """

    def __init__(
        self,
        project_root: Path,
        connectors: Mapping[str, ConnectorBase],
        config: ReconcileConfig,
        *,
        headless: bool = False,
        drafter: LLMDrafter | None = None,
    ) -> None:
        self._project_root = project_root
        self._connectors = connectors
        self._config = config
        # Headless mode hard-pins NullLLMDrafter regardless of what drafter was injected
        # (SPEC-E4 §9 / S9-AC1): the deterministic builder core is guaranteed LLM-free even
        # if a caller mistakenly passes a real drafter in headless mode.
        # Interactive mode uses the injected drafter (or falls back to NullLLMDrafter when
        # None, e.g. "no models configured" operating point).
        self._builder = ContextBuilder(NullLLMDrafter()) if headless else ContextBuilder(drafter)
        self._engine = ReconciliationEngine(config)
        self._emitter = DiffEmitter()

    async def run(self, evidence: list[EvidenceItem], *, dry_run: bool = False) -> PipelineResult:
        """Run all four stages over ``evidence`` and return the reviewable result (SPEC-E4 §2).

        Propose-only by default (§5.5): emits reviewable diffs and edits no committed file in
        place, beyond the audit trail, the no-op ``last_validated_at`` refresh, and any
        auto-apply-eligible entry the policy permits (none under the default config). Validation
        (§10) runs before emission and raises ``SchemaMismatch`` / ``ValidationFailed`` so an
        invalid proposed state never reaches a diff (S8). ``dry_run`` suppresses every write.
        """
        emission, skipped = await self._emit(evidence)
        if not dry_run:
            self._persist(evidence, emission)
        return PipelineResult(emission=emission, skipped=skipped)

    async def bootstrap(self, connection: str) -> PipelineResult:
        """Fast initial bootstrap for one connection (SPEC-E4 §8).

        Tier-1 live introspection of ``connection``, drafted deterministically into semantic
        sources and **written** directly — the fresh-project path that supersedes the E1 thin
        scaffold. Validation still gates the write (S8); knowledge drafting and cross-source
        reconciliation are part of the full ingest, not the bootstrap.
        """
        from canon.ingestion.source import evidence_from_introspection

        connector = self._connectors[connection]
        evidence = await evidence_from_introspection(connector, connection)

        emission, skipped = await self._emit(evidence)
        self._persist(evidence, emission)
        self._write_diffs(d for d in emission.diffs if d.op is ProposalOp.ADD)
        return PipelineResult(emission=emission, skipped=skipped)

    async def _emit(
        self, evidence: list[EvidenceItem]
    ) -> tuple[EmissionResult, list[SkippedEvidence]]:
        """Stages 1–4: build → reconcile → validate → emit (no side effects)."""
        build = await self._builder.build(evidence)
        report = self._engine.reconcile(build.proposals, DiskAcceptedStore(self._project_root))
        gate = ValidationGate(self._project_root, self._connectors, evidence)
        await gate.validate(build.proposals)  # raises before emit (S8)
        emission = self._emitter.emit(report)
        return emission, build.skipped

    def _persist(self, evidence: list[EvidenceItem], emission: EmissionResult) -> None:
        """Side effects of a non-dry run: audit trail, no-op refresh, bounded auto-apply (§6/§7)."""
        AuditTrailWriter.for_project(self._project_root).write(evidence, emission.report)
        self._refresh_no_ops(emission.report)
        self._write_diffs(d for d in emission.diffs if d.auto_apply)

    def _refresh_no_ops(self, report: ReconciliationReport) -> None:
        """Refresh ``last_validated_at`` on unchanged targets (SPEC-E4 §5.2 / §7, S6-AC1).

        The only in-place mutation an unchanged run performs: a no-op proposal touched no
        content, so we bump the freshness stamp on the accepted file and rewrite it. This
        changes neither a proposal nor a decision, so headless determinism holds (S9-AC1).
        """
        validated_at = datetime.now(UTC)
        for entry in report.entries:
            if entry.decision is not ReconciliationDecision.NO_OP:
                continue
            path = self._project_root / entry.target
            if not path.exists():
                continue
            source = load_semantic_source(path)
            refreshed = source.model_copy(
                update={"meta": source.meta.model_copy(update={"last_validated_at": validated_at})}
            )
            path.write_text(dump_semantic_source(refreshed))

    def _write_diffs(self, diffs: Iterable[EmittedDiff]) -> None:
        """Apply emitted diffs by writing their rendered ``after`` state to the target file.

        Used for the bootstrap (write every ``add``) and for auto-apply-eligible entries (§5.5);
        both write the exact proposed state the validation gate already accepted.
        """
        write_emitted_diffs(self._project_root, diffs)
