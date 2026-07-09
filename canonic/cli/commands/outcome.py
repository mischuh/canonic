"""``canonic outcome`` — record ground-truth outcome marks on served answers (SPEC-E16 Part 2 §3).

An analyst, CI verdict, or agent self-report marks a served answer correct/incorrect, with
an attribution reason-code when incorrect. This command only *records* the outcome to the
local event log (``.canonic/events.jsonl``); acting on it — e.g. flagging a binding as
contradicted — is E11's job, not this one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.config import find_project_root
from canonic.exc import ValidationFailed
from canonic.instrumentation.events import DiskAnswerEventLog
from canonic.instrumentation.models import (
    AnswerOutcomeEvent,
    OutcomeMarkedBy,
    OutcomeReasonCode,
    OutcomeVerdict,
)
from canonic.instrumentation.report import read_events

app = typer.Typer(name="outcome", help="Record ground-truth outcome marks on served answers.")

_console = Console(soft_wrap=True)


@app.command("mark")
@handle_errors
def mark(
    ctx: typer.Context,
    ref: Annotated[
        str, typer.Option("--ref", help="The AnswerEvent.query_hash this outcome is about.")
    ],
    verdict: Annotated[
        OutcomeVerdict, typer.Option("--verdict", help="Whether the served answer was correct.")
    ],
    reason: Annotated[
        OutcomeReasonCode | None,
        typer.Option(
            "--reason",
            help="Why it was wrong (only 'wrong_definition' can flag the binding for E11).",
        ),
    ] = None,
    by: Annotated[
        OutcomeMarkedBy, typer.Option("--by", help="Who is marking this outcome.")
    ] = OutcomeMarkedBy.ANALYST,
    correction: Annotated[
        str | None,
        typer.Option("--correction", help="Corrected SQL hash or definition reference."),
    ] = None,
) -> None:
    """Append an ``answer_outcome`` event linked to a served answer's ``query_hash``.

    Raises :class:`~canonic.exc.ValidationFailed` (exit 9) when ``--reason`` is given
    together with ``--verdict correct`` — a reason-code only makes sense for an incorrect
    verdict. When ``--verdict incorrect`` is given without ``--reason``, it defaults to
    ``unspecified`` (SPEC-E16 Part 2 §9 — conservative default, E11 weights it low).
    """
    json_output = get_cli_context(ctx).json_output
    root = find_project_root()
    if root is None:
        msg = "no canonic project found; run from inside a project directory"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    try:
        event = AnswerOutcomeEvent(
            ts=datetime.now(UTC).isoformat(),
            ref=ref,
            verdict=verdict,
            reason_code=reason,
            correction=correction,
            marked_by=by,
        )
    except ValidationError as exc:
        raise ValidationFailed(str(exc)) from exc

    known_refs = {ev.query_hash for ev in read_events(root, kind="served_answer")}
    if ref not in known_refs and not json_output:
        _console.print(
            f"[yellow]warning:[/yellow] {ref!r} does not match any recorded AnswerEvent "
            "— recording the outcome anyway"
        )

    DiskAnswerEventLog(root).append(event)

    if json_output:
        typer.echo(json.dumps(event.model_dump(mode="json")))
    else:
        _console.print(f"recorded outcome [bold]{event.verdict.value}[/bold] for {ref}")
        if event.reason_code is not None:
            _console.print(f"  reason: {event.reason_code.value}")
