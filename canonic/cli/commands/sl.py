"""``canonic sl`` — semantic-layer resolve/compile/describe (E7 §3)."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — runtime type for typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import load_service

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

app = typer.Typer(name="sl", help="Semantic layer: resolve, compile, and describe.")

_console = Console()


@app.command("resolve")
@handle_errors
def resolve(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Metric/dimension name.")],
    context: Annotated[
        str | None,
        typer.Option("--context", help="Tag for context-scoped guardrail resolution."),
    ] = None,
) -> None:
    """Resolve a name to its canonical binding (core.resolve).

    With ``--json`` the output matches the MCP ``resolve_metric`` tool payload byte-for-byte.
    """
    binding = load_service(ctx).resolve_metric(name, context=context)
    payload = {"metric": binding.metric, "source": binding.source, "measure": binding.measure}

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _console.print(
        f"[bold]{binding.metric}[/bold]"
        f"  source={binding.source or '(composite)'}"
        f"  measure={binding.measure or '(composite)'}"
        f"  kind={binding.kind.value}"
    )


@app.command("compile")
@handle_errors
def compile_(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option("-f", "--file", help="Semantic query JSON file.", exists=True, readable=True),
    ],
) -> None:
    """Compile a semantic query to SQL + metadata without executing (core.compile).

    With ``--json`` the output matches the MCP ``compile_query`` tool payload byte-for-byte.
    """
    from canonic.compiler import SemanticQuery
    from canonic.core.models import CompileOutput

    sq = SemanticQuery.model_validate_json(file.read_text())
    result = load_service(ctx).compile_query(sq)
    payload = CompileOutput.from_compile_result(result).model_dump(mode="json")

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _render_compile(result)


def _render_compile(result: CompileResult) -> None:
    """Render a CompileResult as human-readable SQL + metadata band."""
    _console.print(f"[bold]dialect:[/bold] {result.dialect}")
    _console.print()
    _console.print(result.sql)
    if result.resolved:
        _console.print("\n[bold]resolved:[/bold]")
        for metric, ref in result.resolved.items():
            _console.print(f"  {metric} → {ref}")
    if result.guardrails_fired:
        _console.print("\n[bold]guardrails:[/bold]")
        for g in result.guardrails_fired:
            _console.print(f"  {g.id}  ({g.kind})")


@handle_errors
@app.command("describe")
def describe(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Metric name or alias.")],
) -> None:
    """Return grain, dimensions, measures, and freshness for one metric (core.describe_metric).

    With ``--json`` the output matches the MCP ``describe_metric`` tool payload byte-for-byte.
    """
    from rich.console import Console

    service = load_service(ctx)
    detail = service.describe_metric(name)
    payload = detail.model_dump(mode="json")

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    console = Console()
    console.print(f"[bold]{detail.metric}[/bold]  source={detail.source or '(composite)'}")
    if detail.grain:
        console.print(f"  grain:      {', '.join(detail.grain)}")
    if detail.dimensions:
        console.print(f"  dimensions: {', '.join(d.name for d in detail.dimensions)}")
    if detail.freshness:
        console.print(f"  freshness:  {detail.freshness.last_validated_at}")
    if detail.examples:
        console.print("  examples:")
        for ex in detail.examples:
            dims = ", ".join(ex.query.dimensions) if ex.query.dimensions else "(none)"
            console.print(f"    - dims=[{dims}]  origin={ex.origin}")
