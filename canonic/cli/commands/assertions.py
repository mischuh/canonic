"""``canonic assert`` — the accuracy harness CI gate (SPEC-Fuller-E15 §3.4, GH-110).

Runs the labeled assertion set through the compiler, compares each result to its expectation
within tolerance, and reports ``accuracy = passed / total``. This is the E16 integration that
turns ">90% accuracy" from aspirational to measured. Used as a CI gate: an accuracy regression
below ``--min-accuracy`` exits 10 (``ASSERTION_FAILED``).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import load_service
from canonic.exc import AssertionFailed

if TYPE_CHECKING:
    from canonic.contracts.assertions import AccuracyReport

_console = Console()


@handle_errors
def assert_(
    ctx: typer.Context,
    min_accuracy: Annotated[
        float,
        typer.Option(
            "--min-accuracy",
            help="Accuracy floor for the CI gate; below it exits 10 (ASSERTION_FAILED).",
            min=0.0,
            max=1.0,
        ),
    ] = 1.0,
) -> None:
    """Run the accuracy harness over all loaded assertions and gate on the result.

    Every executable assertion in ``contracts/assertions/`` is compiled, executed read-only,
    and compared to its expected value within ``tolerance``. The harness reports
    ``accuracy = correct / total``; when it drops below ``--min-accuracy`` (default ``1.0`` —
    every assertion must hold), the command exits 10 with the diverging checks, so a regression
    fails CI (SPEC-Fuller-E15 §3.4 AC2).
    """
    service = load_service(ctx)
    report = asyncio.run(service.run_accuracy_harness())

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(report.to_dict()))
    else:
        _render(report)

    if report.accuracy < min_accuracy:
        first = report.failures[0] if report.failures else None
        raise AssertionFailed(
            f"accuracy {report.accuracy:.1%} below target {min_accuracy:.1%} "
            f"({report.passed}/{report.total} assertions passed)",
            assertion_id=first.assertion_id if first is not None else None,
        )


def _render(report: AccuracyReport) -> None:
    """Print a human-readable accuracy summary and any diverging assertions."""
    _console.print(
        f"accuracy [bold]{report.accuracy:.1%}[/bold] "
        f"({report.passed}/{report.total} assertions passed)"
    )
    for failure in report.failures:
        _console.print(f"  [red]✗[/red] {failure.detail}")
