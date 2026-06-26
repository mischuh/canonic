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
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from canon.config import LOCAL_STATE_DIR

if TYPE_CHECKING:
    from canon.ingestion.emitter import EmissionResult, EmittedDiff

__all__ = [
    "PendingDiffStore",
    "PendingProposalEntry",
    "PendingRun",
    "PendingStatus",
    "ProposalStatus",
    "apply_entry",
    "generate_run_id",
    "latest_run_id",
    "update_status",
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


# ---------------------------------------------------------------------------
# Read / apply layer (GH-150)
# ---------------------------------------------------------------------------


def latest_run_id(project_root: Path) -> str | None:
    """Return the name of the most-recent run directory under ``.canon/pending-diffs/``.

    Run-ids are ISO timestamps so lexicographic order is chronological.  Returns ``None``
    when no runs exist yet.
    """
    pending_root = project_root / LOCAL_STATE_DIR / _PENDING_DIFFS_DIR
    if not pending_root.is_dir():
        return None
    dirs = sorted(d.name for d in pending_root.iterdir() if d.is_dir())
    return dirs[-1] if dirs else None


def _load_yaml(path: Path) -> object:
    """Load a YAML file using the project-standard round-trip loader."""
    yaml = YAML()
    with path.open() as f:
        return yaml.load(f)


class PendingRun:
    """A loaded view of one pending-diff run directory.

    Aligns the ``status.yaml`` proposal list with reconstructed :class:`EmittedDiff` objects
    produced by re-running :class:`~canon.ingestion.emitter.DiffEmitter` over the persisted
    ``report.yaml``.  The order is deterministic: both ``PendingDiffStore.write`` and
    ``DiffEmitter.emit`` walk entries in report order, so ``proposals[i] ↔ diffs[i]``.
    """

    def __init__(
        self,
        run_dir: Path,
        status: PendingStatus,
        diffs: list[EmittedDiff],
    ) -> None:
        self._run_dir = run_dir
        self._status = status
        self._diffs = diffs

    @classmethod
    def load(cls, run_dir: Path) -> PendingRun:
        """Load ``status.yaml`` and reconstruct diffs from ``report.yaml``."""
        from canon.ingestion.emitter import DiffEmitter
        from canon.ingestion.models import ReconciliationReport

        status_data = _load_yaml(run_dir / "status.yaml")
        status = PendingStatus.model_validate(status_data)

        report_data = _load_yaml(run_dir / "report.yaml")
        report = ReconciliationReport.model_validate(report_data)
        emission = DiffEmitter().emit(report)

        if len(status.proposals) != len(emission.diffs):
            raise ValueError(
                f"status.yaml has {len(status.proposals)} proposals but report.yaml "
                f"yielded {len(emission.diffs)} diffs — run directory may be corrupt: {run_dir}"
            )

        return cls(run_dir, status, emission.diffs)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def status(self) -> PendingStatus:
        return self._status

    @property
    def proposals(self) -> list[PendingProposalEntry]:
        return list(self._status.proposals)

    def diff_for(self, entry: PendingProposalEntry) -> EmittedDiff:
        """Return the reconstructed :class:`EmittedDiff` that corresponds to ``entry``."""
        idx = next(i for i, p in enumerate(self._status.proposals) if p.id == entry.id)
        return self._diffs[idx]

    def patch_text(self, entry: PendingProposalEntry) -> str:
        """Read and return the raw unified-diff text for ``entry`` from disk."""
        return Path(entry.diff_file).read_text()


def update_status(run_dir: Path, proposals: list[PendingProposalEntry]) -> None:
    """Persist an updated proposal list back to ``status.yaml`` in ``run_dir``."""
    status_data = _load_yaml(run_dir / "status.yaml")
    run_id = str(status_data["run_id"])  # type: ignore[index]
    status = PendingStatus(run_id=run_id, proposals=proposals)
    (run_dir / "status.yaml").write_text(_dump_yaml(status.model_dump(mode="json")))


def apply_entry(
    project_root: Path,
    run: PendingRun,
    entry: PendingProposalEntry,
    *,
    freeze: bool = False,
) -> None:
    """Apply one proposal from ``run`` to the working directory.

    Writes the target file per the diff's ``op`` (add/edit/prune) using the same
    :func:`~canon.ingestion.pipeline.write_emitted_diffs` function the pipeline uses.
    When ``freeze=True`` the written file additionally gets ``meta.frozen: true`` set so a
    subsequent ``canon ingest`` flags conflicts instead of editing the fact (AC5 / E4 §5.3).
    ``freeze`` is silently ignored for PRUNE ops (there is no file left to annotate).
    """
    from canon.ingestion.models import ProposalOp
    from canon.ingestion.pipeline import write_emitted_diffs
    from canon.semantic.loader import dump_semantic_source, load_semantic_source

    diff = run.diff_for(entry)
    write_emitted_diffs(project_root, [diff])

    if freeze and diff.op is not ProposalOp.PRUNE:
        path = project_root / diff.target
        if path.exists() and diff.target.endswith(".yaml"):
            source = load_semantic_source(path)
            updated_meta = source.meta.model_copy(update={"frozen": True})
            frozen_source = source.model_copy(update={"meta": updated_meta})
            path.write_text(dump_semantic_source(frozen_source))
