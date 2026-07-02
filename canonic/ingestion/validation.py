"""Validation gate — the proposed output state is validated before any diff is emitted (SPEC-E4 §10).

Stage 3 of the ingestion pipeline. Sits between reconciliation (stage 2) and diff emission
(stage 4): a diff that would produce invalid context is rejected here and never emitted. The
gate reuses two existing validators and introduces no new error code, so the frozen serving
contract (SPEC-P0 §6) stays untouched:

- **Schema-validation probe (E2 §5):** for tier 4–6 evidence (``declarative`` / ``sample`` /
  ``hand_authored``), the declared schema must still match the live source — a mismatch is
  ``SCHEMA_MISMATCH``, never silent acceptance.
- **Semantic/contract validation (E5 §7):** reference integrity, types, grain, and
  cross-surface references are checked on the *proposed* (not-yet-committed) file state — a
  failure is ``VALIDATION_FAILED``.

Every violation is aggregated with a precise location and the gate raises them together, so a
reviewer sees all problems at once; on a clean pass it returns ``None`` and the diff may emit.
The gate is a standalone stage, mirroring the other three: the caller runs it between
``ReconciliationEngine.reconcile`` and ``DiffEmitter.emit`` and a raised exception aborts
before emit (S8-AC1/AC2).
"""

from __future__ import annotations

import io
import shutil
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from canonic.connectors.acquisition import probe_schema
from canonic.connectors.base import (
    AcquisitionTier,  # noqa: TC001 — used at runtime in the probe-tier set
    RelationSchema,
    SchemaIntrospectable,
)
from canonic.contracts.validate import validate_contracts
from canonic.exc import ContractError, SchemaMismatch, SemanticSourceError, ValidationFailed
from canonic.ingestion.models import EvidenceKind, ProposalOp
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from canonic.connectors.base import ConnectorBase
    from canonic.ingestion.models import EvidenceItem, Proposal

__all__ = [
    "ValidationGate",
    "ValidationReport",
    "Violation",
    "ViolationKind",
]

# Ladder tiers 4–6 (SPEC-E2 §4): declared schema is unverified until probed against the live
# source. Tiers 1–3 (live / modeling / query_history) are already live-sourced and skip it.
_PROBE_TIERS: frozenset[AcquisitionTier] = frozenset(
    {AcquisitionTier.DECLARATIVE, AcquisitionTier.SAMPLE, AcquisitionTier.HAND_AUTHORED}
)

# The two output surfaces validated by semantic/contract validation; other targets
# (e.g. knowledge/*.md) have no validator and pass through unchanged.
_VALIDATION_SURFACES = ("semantics/", "contracts/")


_PROVENANCE_TIER: dict[Provenance, int] = {
    Provenance.INFERRED: 0,
    Provenance.HUMAN_CURATED: 1,
    Provenance.BOARD_APPROVED: 2,
}


def _file_provenance(path: Path) -> Provenance:
    """Read the provenance from a committed YAML file without full model validation.

    Semantic sources store it at ``meta.provenance``; contract files (metric bindings,
    guardrails) store it at the top level. Falls back to INFERRED when absent.
    """
    yaml = YAML()
    raw: dict[str, object] = yaml.load(path.read_text()) or {}
    meta = raw.get("meta")
    if isinstance(meta, dict) and "provenance" in meta:
        try:
            return Provenance(meta["provenance"])
        except ValueError:
            pass
    prov = raw.get("provenance")
    if isinstance(prov, str):
        try:
            return Provenance(prov)
        except ValueError:
            pass
    return Provenance.INFERRED


def _dump_yaml(content: dict[str, object]) -> str:
    """Serialize a proposal's content fragment to YAML for the proposed-state overlay."""
    yaml = YAML()
    yaml.default_flow_style = False
    buffer = io.StringIO()
    yaml.dump(content, buffer)
    return buffer.getvalue()


class ViolationKind(StrEnum):
    """The check a violation came from. Local to the gate — NOT an ``ErrorCode`` (no new codes)."""

    SCHEMA_MISMATCH = "schema_mismatch"
    VALIDATION_FAILED = "validation_failed"


class Violation(BaseModel):
    """One rejected check with a precise location (SPEC-E4 §10 / S8)."""

    model_config = ConfigDict(frozen=True)

    kind: ViolationKind
    target: str  # the proposal target / offending file
    location: str  # "file:line", a relation, or a named binding — as precise as the source allows
    detail: str


class ValidationReport(BaseModel):
    """Aggregated result of one gate run; empty ``violations`` means the diff may emit."""

    model_config = ConfigDict(frozen=True)

    violations: list[Violation] = []

    @property
    def ok(self) -> bool:
        """True iff no violation was recorded."""
        return not self.violations


class ValidationGate:
    """Validates the proposed output state before emission (SPEC-E4 §10).

    The gate needs more than the proposals themselves: the ``acquisition_tier`` and the
    source ``RelationSchema`` live on the originating ``EvidenceItem`` (recovered by
    ``anchored_to`` fingerprint), and the E5 validators read a project *directory*. These
    are injected so the public call stays ``validate(proposals)``.
    """

    def __init__(
        self,
        project_root: Path,
        connectors: Mapping[str, ConnectorBase],
        evidence: Iterable[EvidenceItem],
    ) -> None:
        self._project_root = project_root
        self._connectors = connectors
        self._evidence_by_fp: dict[str, EvidenceItem] = {
            item.source_fingerprint: item for item in evidence
        }

    async def validate(self, proposals: list[Proposal]) -> None:
        """Run the probe (E2 §5) and semantic/contract validation (E5 §7) on the proposed state.

        Aggregates every violation with its precise location and raises them together —
        ``SchemaMismatch`` when only the probe failed, otherwise ``ValidationFailed`` — so the
        diff is never emitted (S8). Returns ``None`` when the proposed state is valid.
        """
        violations: list[Violation] = []
        violations.extend(await self._probe_violations(proposals))
        violations.extend(self._semantic_contract_violations(proposals))

        report = ValidationReport(violations=violations)
        if not report.ok:
            self._raise(report)

    async def _probe_violations(self, proposals: list[Proposal]) -> list[Violation]:
        """Probe tier 4–6 schema proposals against the live source (SPEC-E2 §5 / S8-AC2)."""
        out: list[Violation] = []
        for proposal in proposals:
            if proposal.op is ProposalOp.PRUNE:
                continue
            evidence = self._anchoring_evidence(proposal)
            if evidence is None or evidence.acquisition_tier not in _PROBE_TIERS:
                continue
            if evidence.kind != EvidenceKind.RELATION_SCHEMA:
                continue

            connector = self._connectors.get(evidence.source)
            if connector is None:
                out.append(
                    Violation(
                        kind=ViolationKind.SCHEMA_MISMATCH,
                        target=proposal.target,
                        location=evidence.source,
                        detail=(
                            f"no connector for source {evidence.source!r}; cannot verify "
                            f"{evidence.acquisition_tier.value} schema against the live source"
                        ),
                    )
                )
                continue

            schema = RelationSchema.model_validate(evidence.payload)
            result = await probe_schema(cast("SchemaIntrospectable", connector), schema)
            if result.ok:
                continue
            try:
                result.raise_for_status()  # reuse its precise declared-vs-observed diff message
            except SchemaMismatch as exc:
                detail = str(exc)
            out.append(
                Violation(
                    kind=ViolationKind.SCHEMA_MISMATCH,
                    target=proposal.target,
                    location=result.relation,
                    detail=detail,
                )
            )
        return out

    def _semantic_contract_violations(self, proposals: list[Proposal]) -> list[Violation]:
        """Validate the proposed file state with the existing E5 validators (SPEC-E5 §7 / S8-AC1).

        The proposed tree is materialized in a temp directory (accepted files + proposals
        applied) so the reused validators see the not-yet-committed state without touching the
        working tree. Semantic loading is fail-fast, so cross-surface contract checks run only
        once the proposed semantics parse cleanly.
        """
        if not any(self._is_validation_target(p.target) for p in proposals):
            return []

        out: list[Violation] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._materialize(root, proposals)
            try:
                list_semantic_sources(root)
            except SemanticSourceError as exc:
                out.append(self._validation_violation(root, exc))
                return out  # contracts cannot be validated against broken semantics
            try:
                validate_contracts(root)
            except (ContractError, SemanticSourceError) as exc:
                out.append(self._validation_violation(root, exc))
        return out

    def _anchoring_evidence(self, proposal: Proposal) -> EvidenceItem | None:
        """The first evidence item a proposal is anchored to, or None if unrecoverable."""
        for fingerprint in proposal.anchored_to:
            evidence = self._evidence_by_fp.get(fingerprint)
            if evidence is not None:
                return evidence
        return None

    def _materialize(self, root: Path, proposals: list[Proposal]) -> None:
        """Build the proposed file tree: accepted semantics/contracts with proposals applied.

        Mirrors the reconciliation tier rule (SPEC-E4 §5.1): an inferred proposal never
        overwrites an existing human-curated or board-approved file in the temp tree, so the
        validated state matches what the reconciliation engine would actually commit.
        """
        for surface in ("semantics", "contracts"):
            src = self._project_root / surface
            if src.is_dir():
                shutil.copytree(src, root / surface)

        for proposal in proposals:
            if not self._is_validation_target(proposal.target):
                continue
            dest = root / proposal.target
            if proposal.op is ProposalOp.PRUNE:
                dest.unlink(missing_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                try:
                    existing_tier = _PROVENANCE_TIER.get(_file_provenance(dest), 0)
                except Exception:  # noqa: BLE001
                    existing_tier = 0
                proposal_tier = _PROVENANCE_TIER.get(proposal.provenance, 0)
                if existing_tier > proposal_tier:
                    continue  # existing higher-tier file wins; reconciliation would CONTRADICTION this
            dest.write_text(_dump_yaml(proposal.content))

    @staticmethod
    def _is_validation_target(target: str) -> bool:
        """True for the semantics/ and contracts/ YAML files the validator validates."""
        return target.endswith(".yaml") and target.startswith(_VALIDATION_SURFACES)

    @staticmethod
    def _validation_violation(root: Path, exc: SemanticSourceError | ContractError) -> Violation:
        """Turn a reused validator's error into a Violation, rebasing its path off the temp dir."""
        message = str(exc).replace(f"{root}/", "")
        location = message.split(": ", 1)[0]
        return Violation(
            kind=ViolationKind.VALIDATION_FAILED,
            target=location.split(":", 1)[0],
            location=location,
            detail=message,
        )

    @staticmethod
    def _raise(report: ValidationReport) -> None:
        """Raise the aggregated violations: SchemaMismatch if probe-only, else ValidationFailed."""
        summary = "; ".join(v.detail for v in report.violations)
        schema_only = all(v.kind is ViolationKind.SCHEMA_MISMATCH for v in report.violations)
        if schema_only:
            raise SchemaMismatch(
                f"validation gate rejected the proposed state: {summary}",
                candidates=report.violations,
            )
        raise ValidationFailed(
            f"validation gate rejected the proposed state: {summary}",
            candidates=report.violations,
        )
