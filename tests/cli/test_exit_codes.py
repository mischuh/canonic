"""The headless exit-code contract: a raised CanonError becomes a structured exit.

This is the basis for the §5.6 CI-gate role; per-error end-to-end coverage lands
with the real capabilities (E2/E5).
"""

import json

import pytest
import typer
from typer.testing import CliRunner

from canon import exc
from canon.cli._errors import CliContext, handle_errors

_CASES: list[tuple[type[exc.CanonError], int]] = [
    (exc.Unresolved, 2),
    (exc.Ambiguous, 3),
    (exc.GuardrailBlock, 8),
    (exc.ValidationFailed, 9),
    (exc.ReadOnlyViolation, 11),
    (exc.SchemaMismatch, 12),
    (exc.ConnectionError, 13),
]


def _app_raising(error_cls: type[exc.CanonError]) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
        ctx.obj = CliContext(json_output=json_output)

    @app.command()
    @handle_errors
    def boom(ctx: typer.Context) -> None:
        raise error_cls("something went wrong")

    return app


@pytest.mark.parametrize(("error_cls", "expected_exit"), _CASES)
def test_canon_error_maps_to_exit_code(error_cls: type[exc.CanonError], expected_exit: int) -> None:
    result = CliRunner().invoke(_app_raising(error_cls), ["boom"])
    assert result.exit_code == expected_exit
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_error_json_payload() -> None:
    result = CliRunner().invoke(_app_raising(exc.GuardrailBlock), ["--json", "boom"])
    assert result.exit_code == 8
    payload = json.loads(result.stderr)
    assert payload == {"code": "guardrail_block", "message": "something went wrong"}


def test_error_text_payload() -> None:
    result = CliRunner().invoke(_app_raising(exc.Unresolved), ["boom"])
    assert result.exit_code == 2
    assert "unresolved" in result.stderr
    assert "something went wrong" in result.stderr
