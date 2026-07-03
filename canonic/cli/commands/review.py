"""``canonic review`` — interactive, item-by-item proposal review (GH-150, E7 §3).

Reads the most-recent (or an explicit) pending-diff run and iterates over all ``pending``
proposals in numeric order, prompting the operator for an action per item.  Resumable:
re-invocation after a quit or crash resumes at the first still-``pending`` item.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from pathlib import Path

import typer

from canonic.cli._errors import handle_errors
from canonic.cli.commands import _console
from canonic.config import LOCAL_STATE_DIR, find_project_root
from canonic.ingestion.pending import (
    PendingProposalEntry,
    PendingRun,
    ProposalStatus,
    apply_entry,
    latest_run_id,
    update_status,
)

logger = logging.getLogger(__name__)

_PENDING_DIFFS_DIR = "pending-diffs"

_ACTIONS = r"\[a]ccept / \[r]eject / \[s]kip / \[f]reeze / \[q]uit"


def _resolve_run_dir(project_root: Path, run_id: str | None) -> Path | None:
    """Return the run directory, picking the most-recent if run_id is None."""
    if run_id is not None:
        candidate = project_root / LOCAL_STATE_DIR / _PENDING_DIFFS_DIR / run_id
        if not candidate.is_dir():
            return None
        return candidate
    resolved = latest_run_id(project_root)
    if resolved is None:
        return None
    return project_root / LOCAL_STATE_DIR / _PENDING_DIFFS_DIR / resolved


@handle_errors
def review(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Explicit run-id to review (defaults to most-recent)."),
    ] = None,
) -> None:
    """Interactively review pending proposals one by one (GH-150, E7 §3).

    Accepts, rejects, skips, or freezes each proposal.  Resumable after quit or crash.
    """
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    run_dir = _resolve_run_dir(root, run_id)
    if run_dir is None:
        if run_id is not None:
            _console.print(f"[red]error:[/red] run-id not found: {run_id!r}")
        else:
            _console.print(
                "[yellow]no pending-diff runs found — run `canonic ingest` first[/yellow]"
            )
        raise typer.Exit(1)

    run = PendingRun.load(run_dir)
    pending = [p for p in run.proposals if p.status is ProposalStatus.PENDING]

    if not pending:
        _console.print("[green]nothing to review — all proposals are already resolved[/green]")
        raise typer.Exit(0)

    total = len(run.proposals)
    resolved_before = total - len(pending)
    logger.info(
        "review: run=%s pending=%d resolved=%d total=%d",
        run_dir.name,
        len(pending),
        resolved_before,
        total,
    )

    _console.print(
        f"[bold]Reviewing run:[/bold] {run_dir.name}  "
        f"({len(pending)} pending, {resolved_before} already resolved)"
    )

    updated: list[PendingProposalEntry] = list(run.proposals)

    for proposal in pending:
        idx = int(proposal.id)
        diff = run.diff_for(proposal)

        _console.print()
        _console.print(
            f"[bold cyan]Proposal {idx}/{total}:[/bold cyan] {proposal.target}  "
            rf"\[[yellow]{proposal.op}[/yellow], "
            f"confidence: {diff.confidence:.2f}, "
            f"{diff.drafted_by.value}]"
        )
        _console.print(run.patch_text(proposal))
        _console.print(_ACTIONS)

        while True:
            raw = typer.prompt(">", prompt_suffix=" ").strip().lower()
            if raw not in {"a", "r", "s", "f", "q"}:
                _console.print(f"[red]unknown action {raw!r}[/red] — choose a/r/s/f/q")
                continue
            break

        if raw == "q":
            _console.print("[yellow]quit — remaining proposals left pending[/yellow]")
            logger.info("review: quit at proposal %d/%d", idx, total)
            break

        new_status: ProposalStatus
        if raw == "a":
            apply_entry(root, run, proposal)
            new_status = ProposalStatus.ACCEPTED
            _console.print(f"[green]accepted[/green] → {proposal.target}")
        elif raw == "f":
            apply_entry(root, run, proposal, freeze=True)
            new_status = ProposalStatus.FROZEN
            _console.print(f"[green]frozen[/green] → {proposal.target}")
        elif raw == "r":
            new_status = ProposalStatus.REJECTED
            _console.print(f"[red]rejected[/red] → {proposal.target}")
        else:
            new_status = ProposalStatus.PENDING
            _console.print(f"[dim]skipped[/dim] → {proposal.target}")
        logger.debug(
            "review: proposal=%s target=%s -> %s", proposal.id, proposal.target, new_status.value
        )

        if raw != "s":
            i = next(j for j, p in enumerate(updated) if p.id == proposal.id)
            updated[i] = proposal.model_copy(update={"status": new_status})
            update_status(run_dir, updated)
