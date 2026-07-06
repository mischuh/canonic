"""``canonic eval`` — the tested local-model baseline harness (SPEC-E10 §7, GH-66).

Operator command: it makes live model calls, so it is not run in CI (only the deterministic
harness internals are unit-tested). ``baseline`` runs candidate models through the real ``draft``
path over a labeled set and writes the per-release baseline doc.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.eval.candidates import load_candidates
from canonic.eval.dataset import (
    default_dataset_path,
    default_reconcile_dataset_path,
    load_grain_cases,
    load_reconcile_cases,
)
from canonic.eval.harness import (
    DEFAULT_ADHERENCE_FLOOR,
    _default_reconcile_drafter_factory,
    run_baseline,
)
from canonic.eval.report import render_markdown
from canonic.runtime.resolver import Task

app = typer.Typer(
    name="eval",
    help="Evaluate local models against the tested baseline.",
)

_console = Console(soft_wrap=True)


@app.command("baseline")
@handle_errors
def baseline(
    ctx: typer.Context,
    candidates: Annotated[
        Path,
        typer.Option("--candidates", "-c", help="YAML list of openai_compatible candidate models."),
    ],
    dataset: Annotated[
        Path | None,
        typer.Option("--dataset", "-d", help="Labeled JSONL set (defaults to the shipped set)."),
    ] = None,
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Where to write the markdown baseline doc."),
    ] = Path("reports/baseline-models.md"),
    task: Annotated[
        str,
        typer.Option("--task", help="Task to evaluate (only 'draft' has a live call site in v1)."),
    ] = Task.DRAFT.value,
    adherence_floor: Annotated[
        float,
        typer.Option("--adherence-floor", help="Min structured-output adherence to recommend."),
    ] = DEFAULT_ADHERENCE_FLOOR,
) -> None:
    """Run candidate models through the labeled set and publish the baseline."""
    named = load_candidates(candidates)

    if task == Task.RECONCILE.value:
        reconcile_cases = load_reconcile_cases(
            dataset if dataset is not None else default_reconcile_dataset_path()
        )
        report = asyncio.run(
            run_baseline(
                named,
                reconcile_cases,
                task=Task.RECONCILE,
                drafter_factory=_default_reconcile_drafter_factory,
                adherence_floor=adherence_floor,
            )
        )
        if get_cli_context(ctx).json_output:
            typer.echo(json.dumps(report.model_dump(mode="json")))
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(reconcile_report=report), encoding="utf-8")
    else:
        cases = load_grain_cases(dataset if dataset is not None else default_dataset_path())
        report = asyncio.run(
            run_baseline(named, cases, task=Task.DRAFT, adherence_floor=adherence_floor)
        )
        if get_cli_context(ctx).json_output:
            typer.echo(json.dumps(report.model_dump(mode="json")))
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(report), encoding="utf-8")

    _console.print(f"baseline written to [bold]{out}[/bold]")
    for summary in report.summaries:
        mark = " [green]✓ recommended[/green]" if summary.name == report.recommended else ""
        _console.print(
            f"  {summary.name}: accuracy {summary.accuracy:.0%}, "
            f"structured-output {summary.schema_adherence:.0%}, "
            f"p50 {summary.p50_latency_ms:.0f} ms{mark}"
        )
    if report.recommended is None:
        _console.print("  [yellow]no candidate cleared the structured-output floor[/yellow]")
