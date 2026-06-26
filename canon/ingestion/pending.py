"""Pending-diff persistence — bridge between canon ingest and a later review/apply (SPEC-E4 §6, E1 §7).

After each ingest run the pipeline writes a timestamped directory under ``.canon/pending-diffs/``
so the emitted proposals survive process exit and a subsequent ``canon review`` / ``canon apply``
can act on them without re-running the reconciliation engine.

Layout written per run::

    .canon/pending-diffs/<run-id>/
    ├── report.yaml          # full ReconciliationReport
    ├── status.yaml          # per-proposal review state (initially all ``pending``)
    └── proposals/
        ├── 0001-semantics-pg-orders.yaml.diff
        └── 0002-knowledge-refunds.md.diff

``<run-id>`` is timestamp-derived (``20260626T143201Z``) so concurrent runs get independent
slots with no merging required (SPEC-E4 §6, GH-149).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from canon.config import LOCAL_STATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from canon.ingestion.emitter import EmissionResult

__all__ = [
    "PendingDiffStore",
    "PendingProposalEntry",
    "PendingStatus",
    "ProposalStatus",
    "generate_run_id",
]

_PENDING_DIFFS_DIR = "pending-diffs"


def generate_run_id() -> str:
    """Return a timestamp-derived run identifier (UTC, ``%Y%m%dT%H%M%SZ``)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _slug(target: str) -> str:
    """Convert a target path to a filesystem-safe slug (``/`` → ``-``)."""
    return target.replace("/", "-")


def _dump_yaml(data: object) -> str:
    """Serialise ``data`` to a YAML string using the project-standard round-trip style."""
    yaml = YAML()
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


class ProposalStatus(StrEnum):
    """Review state of an individual proposal (SPEC-E4 §6, GH-149)."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FROZEN = "frozen"


class PendingProposalEntry(BaseModel):
    """Metadata for one proposal in ``status.yaml``."""

    model_config = ConfigDict(frozen=True)

    id: str
    target: str
    op: str
    diff_file: str
    status: ProposalStatus = ProposalStatus.PENDING


class PendingStatus(BaseModel):
    """The full ``status.yaml`` document: run metadata + per-proposal state."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    proposals: list[PendingProposalEntry] = []


class PendingDiffStore:
    """Persists emitted diffs to ``.canon/pending-diffs/<run-id>/`` (SPEC-E4 §6, GH-149).

    Each call to :meth:`write` is independent — it creates a fresh timestamped directory so
    concurrent or sequential ingest runs never collide.  The directory is never deleted by
    this class (audit artefact, GH-149 AC3).
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root / LOCAL_STATE_DIR / _PENDING_DIFFS_DIR

    def write(self, run_id: str, emission: EmissionResult) -> Path:
        """Materialise the pending-diff tree for ``run_id`` and return the run directory.

        If ``run_id`` already exists (same-second collision) a numeric suffix is appended
        until a free slot is found.
        """
        run_dir = self._unique_run_dir(run_id)
        proposals_dir = run_dir / "proposals"
        proposals_dir.mkdir(parents=True)

        entries: list[PendingProposalEntry] = []
        for idx, diff in enumerate(emission.diffs, start=1):
            proposal_id = f"{idx:04d}"
            filename = f"{proposal_id}-{_slug(diff.target)}.diff"
            (proposals_dir / filename).write_text(diff.patch)
            entries.append(
                PendingProposalEntry(
                    id=proposal_id,
                    target=diff.target,
                    op=diff.op.value,
                    diff_file=str(proposals_dir / filename),
                    status=ProposalStatus.PENDING,
                )
            )

        (run_dir / "report.yaml").write_text(_dump_yaml(emission.report.model_dump(mode="json")))
        status = PendingStatus(run_id=run_id, proposals=entries)
        (run_dir / "status.yaml").write_text(_dump_yaml(status.model_dump(mode="json")))

        return run_dir

    def _unique_run_dir(self, run_id: str) -> Path:
        """Return a non-existing directory path derived from ``run_id``."""
        candidate = self._root / run_id
        if not candidate.exists():
            return candidate
        suffix = 2
        while True:
            candidate = self._root / f"{run_id}-{suffix:03d}"
            if not candidate.exists():
                return candidate
            suffix += 1
