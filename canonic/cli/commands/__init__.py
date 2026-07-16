"""CLI subcommand groups. Each module exposes a ``typer.Typer()`` named ``app``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from ruamel.yaml import YAML

from canonic.cli._errors import get_cli_context
from canonic.compiler import SemanticQuery
from canonic.compiler.query import parse_filter_flag

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.core.service import CanonicService

_console = Console()


def load_raw_config(path: Path) -> Any:
    """Load ``canonic.yaml`` in ruamel round-trip mode, preserving comments/formatting."""
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f)


def write_raw_config(path: Path, raw: Any) -> None:
    """Write a ruamel round-trip document back to ``canonic.yaml``."""
    yaml = YAML()
    yaml.default_flow_style = False
    with open(path, "w") as f:
        yaml.dump(raw, f)


def not_implemented(ctx: typer.Context, feature: str) -> None:
    """Print a uniform ``not implemented yet`` notice and exit 0 (no traceback).

    Stub for capability commands not yet wired up: currently just ``completion``
    (no backing implementation).
    """
    json_output = get_cli_context(ctx).json_output
    if json_output:
        typer.echo(json.dumps({"status": "not_implemented", "feature": feature}))
    else:
        _console.print(f"[yellow]{feature}[/yellow]: not implemented yet")
    raise typer.Exit(0)


def load_service(ctx: typer.Context) -> CanonicService:
    """Locate the enclosing canonic project and build its :class:`CanonicService`.

    Capability commands (``query``, ``sql``) share this so project discovery and
    service wiring live in one place. Exits 1 with a clear message — not a
    traceback — when run outside a project.
    """
    from canonic.config import find_project_root
    from canonic.core.service import CanonicService

    root = find_project_root()
    if root is None:
        msg = "no canonic project found; run from inside a project directory"
        if get_cli_context(ctx).json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    try:
        from canonic.config import load_config
        from canonic.log import _effective_log_params, configure_logging

        cfg = load_config(root / "canonic.yaml")
        level, file, format = _effective_log_params(
            cfg.logging.level, cfg.logging.file, cfg.logging.format
        )
        configure_logging(level=level, file=file, format=format)
    except Exception:
        pass  # CanonicService.from_project below will fail with a clearer error if config is broken

    return CanonicService.from_project(root)


def _expand_csv(values: list[str] | None) -> list[str]:
    """Flatten repeated and/or comma-separated ``--flag`` occurrences into one list."""
    return [v.strip() for item in values or [] for v in item.split(",") if v.strip()]


def build_semantic_query(
    file: Path | None,
    metrics: list[str] | None,
    dimensions: list[str] | None,
    filters: list[str] | None,
) -> SemanticQuery:
    """Build a :class:`SemanticQuery` from ``-f``/``--file`` or the inline flag set.

    Exactly one of the two input modes must be used — ``query``/``sl compile`` share
    this so both commands resolve flags to the identical object the JSON-file path
    would deserialize (SPEC-E7-E8 §3, S14).
    """
    flags_given = bool(metrics or dimensions or filters)
    if file is not None and flags_given:
        raise typer.BadParameter(
            "-f/--file and --metrics/--dimensions/--filter are mutually exclusive"
        )
    if file is None and not flags_given:
        raise typer.BadParameter("either -f/--file or --metrics is required")

    if file is not None:
        return SemanticQuery.model_validate_json(file.read_text())

    try:
        parsed_filters = [parse_filter_flag(f) for f in filters or []]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    return SemanticQuery(
        metrics=_expand_csv(metrics), dimensions=_expand_csv(dimensions), filters=parsed_filters
    )
