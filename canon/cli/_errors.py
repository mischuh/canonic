"""Shared CLI plumbing: the global context object and structured error handling.

Adapters do transport translation only (SPEC §2.1): here we turn a raised
``CanonError`` into the headless exit code from the canonical registry (§6.1) and
print a structured ``{code, message}`` payload — JSON under ``--json``, Rich text
otherwise — instead of leaking a traceback.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

import typer
from rich.console import Console

from canon.exc import CanonError

_err_console = Console(stderr=True)


@dataclass
class CliContext:
    """Per-invocation state shared from the root callback to every subcommand."""

    json_output: bool = False


def get_cli_context(ctx: typer.Context) -> CliContext:
    """Return the ``CliContext`` for this invocation, creating a default if absent."""
    if not isinstance(ctx.obj, CliContext):
        ctx.obj = CliContext()
    return ctx.obj


def emit_error(err: CanonError, *, json_output: bool) -> None:
    """Print a structured error to stderr in the requested format."""
    code = err.code.value if err.code is not None else "internal_error"
    message = str(err) or err.__class__.__name__
    payload: dict[str, Any] = {"code": code, "message": message}
    # ASSERTION_FAILED carries which check diverged (SPEC-Fuller-E15 §3.3).
    assertion_id = getattr(err, "assertion_id", None)
    if assertion_id is not None:
        payload["assertion_id"] = assertion_id
    if json_output:
        sys.stderr.write(json.dumps(payload) + "\n")
    else:
        _err_console.print(f"[red]error[/red] [bold]{code}[/bold]: {message}")
        candidates = err.candidates
        if candidates:
            for i, c in enumerate(candidates, 1):
                if hasattr(c, "route"):
                    _err_console.print(f"  path {i}: {c.route}", markup=False)
                    _err_console.print(f"    via: {c.via}", markup=False)
                elif isinstance(c, (list, tuple)):
                    owner = getattr(err, "owner", "")
                    prefix = f"{owner} → " if owner else ""
                    _err_console.print(f"  path {i}: {prefix}{' → '.join(c)}", markup=False)
            if candidates and hasattr(candidates[0], "via"):
                _err_console.print(
                    '  hint: re-issue with "via": <chosen path\'s via list> to select a path',
                    markup=False,
                )
            elif candidates and isinstance(candidates[0], (list, tuple)):
                _err_console.print(
                    '  hint: add "via": ["<first-hop>"] to your query to select a path',
                    markup=False,
                )


def handle_errors[F: Callable[..., Any]](func: F) -> F:
    """Wrap a command so a raised ``CanonError`` becomes a structured exit.

    The wrapped command must declare a ``ctx: typer.Context`` parameter so the
    handler can read ``--json`` mode from the shared ``CliContext``.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        if ctx is None:
            ctx = next((a for a in args if isinstance(a, typer.Context)), None)
        json_output = get_cli_context(ctx).json_output if ctx is not None else False
        try:
            return func(*args, **kwargs)
        except CanonError as err:
            emit_error(err, json_output=json_output)
            raise typer.Exit(err.exit_code) from err

    return wrapper  # type: ignore[return-value]
