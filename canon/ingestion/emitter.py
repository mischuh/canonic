"""Diff emission & audit trail — ReconciliationReport → reviewable diffs (SPEC-E4 §6).

Stage 4 of the ingestion pipeline. Turns a :class:`ReconciliationReport` into a set of
evidence-anchored, reviewable diffs against committed files, surfaces contradictions as
review notes (never hard failures, §5.4), and records the audit trail that makes every
committed change traceable to the evidence and decision that produced it.

The split mirrors the upstream stages: :class:`DiffEmitter` is a pure function of its
report — it never touches the filesystem, so identical reports yield byte-identical output
(headless determinism, §9 / S9-AC1). The side-effecting audit trail (committed scan
snapshot under ``raw-sources/`` and the local, git-ignored event log under ``.canon/``) is
written through injected :class:`SnapshotStore` / :class:`EventLog` protocols, exactly as
the builder injects ``LLMDrafter`` and the engine injects ``AcceptedStore``.
"""

from __future__ import annotations

import difflib
import io
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from canon.config import LOCAL_STATE_DIR
from canon.ingestion.models import (
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.semantic.models import Provenance  # noqa: TC001 — Pydantic resolves at runtime

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from canon.ingestion.models import EvidenceItem

__all__ = [
    "AuditTrailWriter",
    "ContradictionNote",
    "DiffEmitter",
    "DiffFormat",
    "DiskEventLog",
    "DiskSnapshotStore",
    "EmissionResult",
    "EmittedDiff",
    "EventLog",
    "SnapshotStore",
]

#: File names for the two audit-trail artefacts (SPEC-E4 §6).
_SNAPSHOT_FILE = "evidence.jsonl"
_EVENT_LOG_FILE = "ingest-events.jsonl"
#: Committed, reproducible scan-snapshot root (SPEC-E1 §2 / SPEC-E4 §6).
_RAW_SOURCES_DIR = "raw-sources"

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DiffFormat(StrEnum):
    """The patch shape a target file takes (SPEC-E4 §6).

    Derived from the target path: ``semantics/*.yaml`` patches are YAML, ``knowledge/*.md``
    patches are Markdown. The format only labels how ``before``/``after`` were rendered; the
    ``patch`` itself is always a unified text diff.
    """

    YAML = "yaml"
    MARKDOWN = "markdown"


def _format_for(target: str) -> DiffFormat:
    """Pick the render format from a target's suffix (SPEC-E4 §6)."""
    return DiffFormat.MARKDOWN if target.endswith(".md") else DiffFormat.YAML


# The file action a diff represents is the reconciliation decision, not the builder's
# proposal op (SPEC-E4 §5.2 / §6); only emitting decisions appear here.
_OP_FOR_DECISION: dict[ReconciliationDecision, ProposalOp] = {
    ReconciliationDecision.ADD: ProposalOp.ADD,
    ReconciliationDecision.EDIT: ProposalOp.EDIT,
    ReconciliationDecision.PRUNE: ProposalOp.PRUNE,
}


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class EmittedDiff(BaseModel):
    """One reviewable, evidence-anchored change against a committed file (SPEC-E4 §6).

    ``before``/``after`` are the rendered file states (``None`` on the side that does not
    exist for an add or prune); ``patch`` is the unified text diff a reviewer reads.
    ``anchored_to`` carries the evidence fingerprints the change derives from so every diff
    traces back to the snapshot (S7-AC1).
    """

    model_config = ConfigDict(frozen=True)

    target: str
    op: ProposalOp
    format: DiffFormat
    before: str | None
    after: str | None
    patch: str
    anchored_to: list[str] = []
    provenance: Provenance
    confidence: float
    auto_apply: bool = False


class ContradictionNote(BaseModel):
    """A flagged contradiction surfaced as a review annotation, not a failure (SPEC-E4 §5.4).

    Rides alongside the diff set (a PR comment in headless mode) for a human to resolve;
    both sides are recorded so neither silently wins (S4). ``existing_frozen`` marks that the
    conflict was driven by a frozen fact (S3).
    """

    model_config = ConfigDict(frozen=True)

    target: str
    recommended_action: str | None = None
    incoming: dict[str, Any]
    incoming_provenance: Provenance
    existing: dict[str, Any] | None = None
    existing_provenance: Provenance | None = None
    existing_frozen: bool = False


class EmissionResult(BaseModel):
    """The reviewable output of one emit run: diffs, contradiction notes, and the report.

    Serializes two ways (SPEC-E4 §6): :meth:`to_json` for CI/machine consumption and
    :meth:`render_markdown` for the human review surface (PR body / console).
    """

    model_config = ConfigDict(frozen=True)

    diffs: list[EmittedDiff] = []
    notes: list[ContradictionNote] = []
    report: ReconciliationReport

    def to_json(self) -> str:
        """Machine-readable JSON for CI — deterministic key order (SPEC-E4 §6)."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=2)

    def render_markdown(self) -> str:
        """Human-readable summary for the review surface (SPEC-E4 §6).

        Leads with the decision counts, then the diff set (each with its evidence anchors),
        then contradictions as review notes — never as errors (§5.4).
        """
        lines: list[str] = ["# Ingest reconciliation summary", ""]

        counts = self.report.summary
        lines.append("## Decisions")
        for decision in ReconciliationDecision:
            lines.append(f"- {decision.value}: {counts[decision.value]}")
        lines.append("")

        lines.append(f"## Diffs ({len(self.diffs)})")
        for diff in self.diffs:
            anchors = ", ".join(diff.anchored_to) or "—"
            lines.append(f"### `{diff.target}` ({diff.op.value})")
            lines.append(f"- anchored_to: {anchors}")
            lines.append(f"- provenance: {diff.provenance.value}, confidence: {diff.confidence}")
            lines.append("")
            lines.append("```diff")
            lines.append(diff.patch.rstrip("\n"))
            lines.append("```")
            lines.append("")

        lines.extend(self._contradiction_lines())
        return "\n".join(lines)

    def render_contradictions(self) -> str:
        """Standalone markdown for the contradiction notes — the headless PR review block (§5.4).

        Posted as a single review comment on the auto-PR so each flagged contradiction reaches a
        human with both sides and a recommended action, separate from the diff body.
        """
        return "\n".join(self._contradiction_lines())

    def _contradiction_lines(self) -> list[str]:
        """Render the ``## Contradictions`` section shared by the body and the review comment."""
        lines = [f"## Contradictions ({len(self.notes)})"]
        for note in self.notes:
            existing = note.existing_provenance.value if note.existing_provenance else "—"
            frozen = " (frozen)" if note.existing_frozen else ""
            lines.append(f"### `{note.target}`{frozen}")
            lines.append(f"- existing: {existing}, incoming: {note.incoming_provenance.value}")
            lines.append(f"- recommended action: {note.recommended_action or '—'}")
            lines.append("")
        return lines


# ---------------------------------------------------------------------------
# Pure diff emitter
# ---------------------------------------------------------------------------


class DiffEmitter:
    """Turns a :class:`ReconciliationReport` into reviewable diffs and notes (SPEC-E4 §6).

    Pure and free of file I/O: ``emit`` reads only the report (whose entries already carry
    the loaded ``existing`` content from reconciliation), so identical reports produce
    byte-identical output (headless determinism, §9 / S9-AC1). Contradictions become review
    notes, never raised errors (§5.4).
    """

    def emit(self, report: ReconciliationReport) -> EmissionResult:
        """Produce the diff set and contradiction notes for ``report`` (SPEC-E4 §6).

        Walks entries in report order (deterministic). ``add``/``edit``/``prune`` become an
        :class:`EmittedDiff`; ``no_op`` produces nothing (idempotency, S6); ``contradiction``
        becomes a :class:`ContradictionNote` (§5.4 / S2–S4).
        """
        diffs: list[EmittedDiff] = []
        notes: list[ContradictionNote] = []

        for entry in report.entries:
            if entry.decision is ReconciliationDecision.NO_OP:
                continue
            if entry.decision is ReconciliationDecision.CONTRADICTION:
                notes.append(self._note(entry))
            else:
                diffs.append(self._diff(entry))

        return EmissionResult(diffs=diffs, notes=notes, report=report)

    def _diff(self, entry: ReconciliationEntry) -> EmittedDiff:
        """Render one add/edit/prune entry into an :class:`EmittedDiff`.

        The diff's ``op`` is the reconciliation *decision* — the action against the committed
        file — not the builder's proposal op (which is ``add`` for every relation schema,
        even when the engine resolves it to an edit, SPEC-E4 §5.2).
        """
        target = entry.target
        fmt = _format_for(target)
        op = _OP_FOR_DECISION[entry.decision]

        if op is ProposalOp.PRUNE:
            before = self._render(entry.existing, fmt)
            after = None
        elif op is ProposalOp.ADD:
            before = None
            after = self._render(entry.proposal.content, fmt)
        else:
            before = self._render(entry.existing, fmt)
            after = self._render(entry.proposal.content, fmt)

        return EmittedDiff(
            target=target,
            op=op,
            format=fmt,
            before=before,
            after=after,
            patch=self._patch(target, before, after),
            anchored_to=list(entry.proposal.anchored_to),
            provenance=entry.proposal.provenance,
            confidence=entry.proposal.confidence,
            auto_apply=entry.auto_apply,
        )

    @staticmethod
    def _note(entry: ReconciliationEntry) -> ContradictionNote:
        """Render one contradiction entry into a review note (SPEC-E4 §5.4)."""
        return ContradictionNote(
            target=entry.target,
            recommended_action=entry.recommended_action,
            incoming=entry.proposal.content,
            incoming_provenance=entry.proposal.provenance,
            existing=entry.existing,
            existing_provenance=entry.existing_provenance,
            existing_frozen=entry.existing_frozen,
        )

    @staticmethod
    def _patch(target: str, before: str | None, after: str | None) -> str:
        """Unified text diff between the two rendered file states (SPEC-E4 §6)."""
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = (after or "").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before_lines, after_lines, fromfile=f"a/{target}", tofile=f"b/{target}"
            )
        )

    @staticmethod
    def _render(content: dict[str, Any] | None, fmt: DiffFormat) -> str:
        """Render proposed/existing content as the target's file body (SPEC-E4 §6).

        YAML reuses the round-trippable dump style of
        :func:`canon.semantic.loader.dump_semantic_source`; Markdown emits the page's
        ``body`` (knowledge pages are not yet built — forward-looking, §1).
        """
        if content is None:
            return ""
        if fmt is DiffFormat.MARKDOWN:
            body = content.get("body", "")
            return str(body)
        yaml = YAML()
        yaml.default_flow_style = False
        buffer = io.StringIO()
        yaml.dump(content, buffer)
        return buffer.getvalue()


# ---------------------------------------------------------------------------
# Audit-trail sinks (file I/O, injected)
# ---------------------------------------------------------------------------


class SnapshotStore(Protocol):
    """Writes the committed scan snapshot of a run's normalized evidence (SPEC-E4 §6)."""

    def write(self, evidence: Iterable[EvidenceItem]) -> None:
        """Persist ``evidence`` so anchored fingerprints resolve back to it (S7-AC1)."""
        ...


class EventLog(Protocol):
    """Appends the per-decision audit log of a run (SPEC-E4 §6, local/git-ignored)."""

    def append(self, entries: Iterable[ReconciliationEntry]) -> None:
        """Record one event per reconciliation decision with its inputs (S7-AC2)."""
        ...


class DiskSnapshotStore:
    """Writes ``raw-sources/<connection-id>/evidence.jsonl`` — committed, reproducible (§6).

    Evidence is grouped by ``source`` (the connection id) and written one item per line in a
    deterministic order, so a re-run with identical evidence yields byte-identical snapshots
    (S9-AC1) and every ``anchored_to`` fingerprint resolves to a line here (S7-AC1).
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root / _RAW_SOURCES_DIR

    def write(self, evidence: Iterable[EvidenceItem]) -> None:
        by_source: dict[str, list[EvidenceItem]] = {}
        for item in evidence:
            by_source.setdefault(item.source, []).append(item)

        for source, items in by_source.items():
            directory = self._root / source
            directory.mkdir(parents=True, exist_ok=True)
            ordered = sorted(items, key=lambda i: (i.kind, i.source_fingerprint))
            lines = [json.dumps(item.model_dump(mode="json"), sort_keys=True) for item in ordered]
            (directory / _SNAPSHOT_FILE).write_text("\n".join(lines) + "\n")


class DiskEventLog:
    """Appends ``.canon/ingest-events.jsonl`` — one event per decision, local only (§6).

    Each event records the decision type, target, tier (provenance), confidence, and anchored
    evidence fingerprints (S7-AC2). The directory is git-ignored (``LOCAL_STATE_DIR``), so the
    log never enters version control.
    """

    def __init__(self, project_root: Path) -> None:
        self._path = project_root / LOCAL_STATE_DIR / _EVENT_LOG_FILE

    def append(self, entries: Iterable[ReconciliationEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        recorded_at = datetime.now(UTC).isoformat()
        with self._path.open("a") as f:
            for entry in entries:
                f.write(json.dumps(self._event(entry, recorded_at), sort_keys=True) + "\n")

    @staticmethod
    def _event(entry: ReconciliationEntry, recorded_at: str) -> dict[str, Any]:
        return {
            "recorded_at": recorded_at,
            "decision": entry.decision.value,
            "target": entry.target,
            "op": entry.proposal.op.value,
            "provenance": entry.proposal.provenance.value,
            "confidence": entry.proposal.confidence,
            "anchored_to": list(entry.proposal.anchored_to),
            "drafted_by": entry.proposal.drafted_by.value,
            "auto_apply": entry.auto_apply,
            "low_confidence": entry.low_confidence,
            "existing_frozen": entry.existing_frozen,
        }


class AuditTrailWriter:
    """Drives both audit-trail sinks for one ingest run (SPEC-E4 §6).

    Thin orchestration over an injected :class:`SnapshotStore` and :class:`EventLog`; the
    convenience constructor :meth:`for_project` wires the on-disk implementations rooted at a
    project directory, while tests inject in-memory fakes.
    """

    def __init__(self, snapshots: SnapshotStore, events: EventLog) -> None:
        self._snapshots = snapshots
        self._events = events

    @classmethod
    def for_project(cls, project_root: Path) -> AuditTrailWriter:
        """Wire the on-disk snapshot store and event log under ``project_root``."""
        return cls(DiskSnapshotStore(project_root), DiskEventLog(project_root))

    def write(self, evidence: Iterable[EvidenceItem], report: ReconciliationReport) -> None:
        """Write the scan snapshot and append the per-decision event log (S7-AC1/AC2)."""
        self._snapshots.write(evidence)
        self._events.append(report.entries)
