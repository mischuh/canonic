"""``canonic decompose``: attribute a metric's change between two periods."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003 — runtime type for the typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli._tenant import TenantOption, cli_tenant_principal
from canonic.cli.commands import _expand_csv, load_service
from canonic.compiler.query import parse_filter_flag
from canonic.core.decompose_models import (
    AdditiveBreakdown,
    DecomposePeriod,
    DecomposeRequest,
    PartsBreakdown,
    ShiftShareBreakdown,
)
from canonic.exc import ValidationFailed

if TYPE_CHECKING:
    from canonic.core.decompose_models import AdditiveRow, DecomposeResult

_console = Console()


@handle_errors
def decompose(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option(
            "-f", "--file", help="Decompose request JSON file.", exists=True, readable=True
        ),
    ] = None,
    metric: Annotated[str | None, typer.Option("--metric", help="Metric name or alias.")] = None,
    period_dimension: Annotated[
        str | None,
        typer.Option("--period-dimension", help="Time dimension the two periods belong to."),
    ] = None,
    base: Annotated[str | None, typer.Option("--base", help="The earlier period.")] = None,
    compare: Annotated[str | None, typer.Option("--compare", help="The later period.")] = None,
    by: Annotated[
        list[str] | None,
        typer.Option("--by", help="Split dimension(s), comma-separated and/or repeatable."),
    ] = None,
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Filter as field=value or field:op:value (repeatable)."),
    ] = None,
    via: Annotated[
        list[str] | None,
        typer.Option("--via", help="Join-path alias prefix, comma-separated and/or repeatable."),
    ] = None,
    top_n: Annotated[
        int | None, typer.Option("--top-n", help="Members listed per dimension.")
    ] = None,
    allow_open_period: Annotated[
        bool,
        typer.Option("--allow-open-period", help="Decompose even when a period has not ended yet."),
    ] = False,
    tenant: TenantOption = None,
) -> None:
    """Attribute the change of one metric between two periods, overall and by member.

    Either ``-f``/``--file`` (a JSON file with the decompose request shape:
    ``{"metric": ..., "period": {"dimension": ..., "base": ..., "compare": ...}, "by": [...]}``)
    or the inline ``--metric``/``--period-dimension``/``--base``/``--compare`` flags, never
    both. With ``--json`` the payload is the same as the MCP ``decompose_change`` tool's.
    """
    principal = cli_tenant_principal(tenant)
    request = _build_request(
        file, metric, period_dimension, base, compare, by, filter_, via, top_n, allow_open_period
    )
    service = load_service(ctx)
    result = asyncio.run(service.decompose(request, principal=principal))

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(result.model_dump(mode="json")))
        return
    _render(result)


def _build_request(
    file: Path | None,
    metric: str | None,
    period_dimension: str | None,
    base: str | None,
    compare: str | None,
    by: list[str] | None,
    filters: list[str] | None,
    via: list[str] | None,
    top_n: int | None,
    allow_open_period: bool,
) -> DecomposeRequest:
    inline = (metric, period_dimension, base, compare)
    flags_given = any(v is not None for v in inline) or bool(by or filters or via)
    if file is not None and flags_given:
        raise typer.BadParameter("-f/--file and the inline request flags are mutually exclusive")
    try:
        if file is not None:
            return DecomposeRequest.model_validate_json(file.read_text())
        if metric is None or period_dimension is None or base is None or compare is None:
            raise typer.BadParameter(
                "either -f/--file or all of --metric, --period-dimension, --base and --compare "
                "are required"
            )
        return DecomposeRequest(
            metric=metric,
            period=DecomposePeriod(dimension=period_dimension, base=base, compare=compare),
            by=_expand_csv(by),
            filters=[parse_filter_flag(f) for f in filters or []],
            via=_expand_csv(via),
            top_n=top_n,
            allow_open_period=allow_open_period,
        )
    except ValidationError as exc:
        raise ValidationFailed(f"invalid decompose request: {exc}") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _number(value: float | None) -> str:
    return "" if value is None else f"{value:,.6g}"


def _member(member: object, rest: bool) -> str:
    if rest:
        return "(rest)"
    return "NULL" if member is None else str(member)


def _render(result: DecomposeResult) -> None:
    period = result.period
    total = result.total
    _console.print(
        f"[bold]{result.metric}[/bold] ({result.kind}, {result.method.value}): "
        f"{period.dimension} {period.base} → {period.compare}: "
        f"{_number(total.base)} → {_number(total.compare)} (Δ {_number(total.delta)})"
    )
    if result.effects:
        table = Table("effect", "metric", "base", "compare", "contribution", header_style="bold")
        for effect in result.effects:
            table.add_row(
                effect.effect.value,
                effect.metric,
                _number(effect.base),
                _number(effect.compare),
                _number(effect.contribution),
            )
        _console.print(table)
    for dimension, breakdown in result.by.items():
        if isinstance(breakdown, AdditiveBreakdown):
            _console.print(_additive_table(dimension, dimension, breakdown.rows))
        elif isinstance(breakdown, ShiftShareBreakdown):
            table = Table(
                dimension,
                "share",
                "rate",
                "mix",
                "rate effect",
                "contribution",
                "presence",
                title=f"by {dimension}",
                header_style="bold",
            )
            for share_row in breakdown.rows:
                table.add_row(
                    _member(share_row.member, share_row.rest),
                    f"{_number(share_row.share_base)} → {_number(share_row.share_compare)}",
                    f"{_number(share_row.rate_base)} → {_number(share_row.rate_compare)}",
                    _number(share_row.mix),
                    _number(share_row.rate),
                    _number(share_row.contribution),
                    share_row.presence.value if share_row.presence is not None else "",
                )
            _console.print(table)
        elif isinstance(breakdown, PartsBreakdown):
            for part, part_breakdown in breakdown.parts.items():
                _console.print(
                    _additive_table(dimension, f"{dimension}: {part}", part_breakdown.rows)
                )
            table = Table(
                dimension,
                "base",
                "compare",
                "delta",
                title=f"by {dimension}: denominator (its own units)",
                header_style="bold",
            )
            for denominator_row in breakdown.denominator.rows:
                table.add_row(
                    _member(denominator_row.member, denominator_row.rest),
                    _number(denominator_row.base),
                    _number(denominator_row.compare),
                    _number(denominator_row.delta),
                )
            _console.print(table)
    for warning in result.metadata.warnings:
        _console.print("[yellow]warning:[/yellow]", end=" ")
        _console.print(warning, markup=False)


def _additive_table(dimension: str, title: str, rows: list[AdditiveRow]) -> Table:
    table = Table(
        dimension, "base", "compare", "contribution", title=f"by {title}", header_style="bold"
    )
    for row in rows:
        table.add_row(
            _member(row.member, row.rest),
            _number(row.base),
            _number(row.compare),
            _number(row.contribution),
        )
    return table
