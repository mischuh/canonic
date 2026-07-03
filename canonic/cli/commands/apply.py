"""``canonic apply`` — batch-apply pending proposals after manual diff editing (GH-150, E7 §3).

Reads ``status.yaml`` from the given run directory and applies every proposal still marked
``pending``.  Proposals whose diff file the user deleted, or that are already in a terminal
state (accepted / rejected / frozen), are silently skipped.  No git interaction (E4 §6).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from canonic.cli._errors import handle_errors
from canonic.cli.commands import _console
from canonic.config import find_project_root
from canonic.ingestion.pending import (
    PendingProposalEntry,
    PendingRun,
    ProposalStatus,
    apply_entry,
    update_status,
)

logger = logging.getLogger(__name__)


@handle_errors
def apply(
    run_dir: Annotated[
        Path,
        typer.Argument(help="Path to the pending-diff run directory to apply."),
    ],
) -> None:
    """Batch-apply all pending proposals from a run directory (GH-150, E7 §3).

    Skips proposals already in a terminal state or whose diff file has been deleted.
    No git interaction — applied files appear as unstaged changes.
    """
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        _console.print(f"[red]error:[/red] run directory not found: {run_dir}")
        raise typer.Exit(1)

    run = PendingRun.load(run_dir)
    logger.info("apply: run_dir=%s proposals=%d", run_dir.name, len(run.proposals))

    applied = 0
    skipped = 0
    updated: list[PendingProposalEntry] = list(run.proposals)

    for proposal in run.proposals:
        if proposal.status is not ProposalStatus.PENDING:
            logger.debug(
                "apply: skipping proposal=%s target=%s (status=%s)",
                proposal.id,
                proposal.target,
                proposal.status.value,
            )
            skipped += 1
            continue
        if not Path(proposal.diff_file).exists():
            logger.debug(
                "apply: skipping proposal=%s target=%s (diff file missing)",
                proposal.id,
                proposal.target,
            )
            skipped += 1
            continue

        apply_entry(root, run, proposal)
        logger.debug("apply: applied proposal=%s target=%s", proposal.id, proposal.target)
        i = next(j for j, p in enumerate(updated) if p.id == proposal.id)
        updated[i] = proposal.model_copy(update={"status": ProposalStatus.ACCEPTED})
        applied += 1

    update_status(run_dir, updated)
    logger.info("apply complete: applied=%d skipped=%d", applied, skipped)
    _console.print(f"[green]applied {applied}[/green], skipped {skipped}")
