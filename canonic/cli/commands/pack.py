"""``canonic pack`` — install a curated context pack (AMENDMENT-context-packs §2, §5).

``canonic pack add`` is the direct, scriptable entry point (CI, ``--params-file``); the
``canonic setup`` wizard's pack branch (``setup.py``) is a second entry point into the
same prompt flow (``_pack_prompts.py``) and install routine (``canonic.packs.install``),
not a separate implementation.
"""

from __future__ import annotations

import json as jsonlib
import os
from pathlib import Path  # noqa: TC003 — used at runtime by typer's Path-typed CLI options
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands._pack_prompts import (
    render_install_result,
    resolve_params_interactively,
    run_and_render_first_answer,
)
from canonic.config import find_project_root, load_config
from canonic.exc import PackError
from canonic.packs.install import check_required_tables, install_pack, resolve_params
from canonic.packs.loader import (
    find_pack_dir,
    list_packs,
    list_readable_packs,
    load_pack_manifest,
)
from canonic.packs.repo import resolve_repo
from canonic.packs.validate import validate_pack

if TYPE_CHECKING:
    from canonic.config import CanonicConfig
    from canonic.packs.loader import SkippedPack
    from canonic.packs.manifest import PackManifest, Variant

_console = Console()

app = typer.Typer(name="pack", help="Install a curated context pack into the project.")

# TODO: swap for the hosted canonic-packs repo URL once pushed somewhere durable
# (AMENDMENT-context-packs §2.5) — a local path works identically in the meantime, so
# this is a one-line change, not an architecture change (--repo already accepts either).
_DEFAULT_REPO_ENV = "CANONIC_PACKS_REPO"
_DEFAULT_REPO = "https://github.com/mischuh/canonic-packs.git"

_RepoOption = Annotated[
    str | None, typer.Option("--repo", help="Pack repo: a git URL or a local path.")
]


def _project_or_exit() -> Path:
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found; run from inside a project directory"
        )
        raise typer.Exit(1)
    return root


def _resolved_repo(repo: str | None, root: Path) -> Path:
    value = repo or os.environ.get(_DEFAULT_REPO_ENV) or _DEFAULT_REPO
    return resolve_repo(value, project_root=root)


@app.command("list")
@handle_errors
def list_(ctx: typer.Context, repo: _RepoOption = None) -> None:
    """List packs available from the configured pack repo."""
    root = _project_or_exit()
    repo_dir = _resolved_repo(repo, root)
    manifests, skipped = list_readable_packs(repo_dir)

    if get_cli_context(ctx).json_output:
        typer.echo(
            jsonlib.dumps(
                {
                    "packs": [
                        {
                            "pack": m.pack,
                            "version": m.version,
                            "description": m.description,
                            "variants": [v.id for v in m.variants],
                        }
                        for m in manifests
                    ],
                    "skipped": [{"pack": s.name, "reason": s.reason} for s in skipped],
                }
            )
        )
        return

    if not manifests:
        _console.print(f"no readable packs found under {repo_dir}")
        _print_skipped(skipped)
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("pack")
    table.add_column("version")
    table.add_column("variants")
    table.add_column("description")
    for m in manifests:
        table.add_row(m.pack, m.version, ", ".join(v.id for v in m.variants), m.description)
    _console.print(table)
    _print_skipped(skipped)


def _print_skipped(skipped: list[SkippedPack]) -> None:
    for s in skipped:
        _console.print(f"[yellow]skipped {s.name}:[/yellow] {s.reason}")
    if skipped:
        _console.print(
            "[dim]A pack that needs a newer canonic fails to load. Upgrade canonic.[/dim]"
        )


@app.command("add")
@handle_errors
def add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Pack name, e.g. 'posthog'.")],
    repo: _RepoOption = None,
    variant_id: Annotated[
        str | None,
        typer.Option("--variant", help="Variant id (default: the pack's only variant)."),
    ] = None,
    connection_id: Annotated[
        str | None,
        typer.Option("--connection", help="Existing connection id to bind."),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Param as KEY=VALUE (repeatable)."),
    ] = None,
    params_file: Annotated[
        Path | None,
        typer.Option("--params-file", help="JSON/YAML file of param values; non-interactive."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the write-preview confirmation.")
    ] = False,
) -> None:
    """Install a context pack: templated semantics/contracts/knowledge for a known system."""
    root = _project_or_exit()
    config = load_config(root / "canonic.yaml")
    repo_dir = _resolved_repo(repo, root)
    pack_dir = find_pack_dir(repo_dir, name)
    manifest = load_pack_manifest(pack_dir)
    manifest.check_compatible()
    variant = _pick_variant(manifest, variant_id)
    manifest = manifest.for_variant(variant.id)

    non_interactive = params_file is not None
    explicit = _explicit_params(param, params_file)
    if connection_id is not None:
        explicit["connection_id"] = connection_id
    explicit.setdefault(
        "connection_id", _pick_connection(config, variant, non_interactive=non_interactive)
    )

    connection = next((c for c in config.connections if c.id == explicit["connection_id"]), None)
    if connection is None:
        raise PackError(f"connection {explicit['connection_id']!r} not found in canonic.yaml")

    if non_interactive:
        params = resolve_params(manifest, explicit)
        check_required_tables(manifest, params, connection)
    else:
        params = resolve_params_interactively(
            root,
            manifest,
            explicit,
            connection.id,
            check_tables=lambda seeded: check_required_tables(manifest, seeded, connection),
        )

    _render_preview(manifest, variant, params)
    if not (yes or non_interactive) and not typer.confirm("Write these files?", default=True):
        _console.print("[yellow]aborted:[/yellow] nothing written")
        raise typer.Exit(0)

    result = install_pack(root, pack_dir, manifest, variant, params)
    render_install_result(result)

    if manifest.first_answer is not None and not run_and_render_first_answer(
        root, manifest.first_answer, params
    ):
        raise typer.Exit(1)


@app.command("validate")
@handle_errors
def validate(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(help="A pack directory (pack.yaml) or a repo root (packs/*/pack.yaml)."),
    ] = Path("."),
    repo: _RepoOption = None,
    variant_id: Annotated[
        str | None,
        typer.Option("--variant", help="Validate only this variant id (default: every one)."),
    ] = None,
) -> None:
    """Validate pack content — no project, no live connection needed (CI-friendly).

    Synthesizes placeholder param values, installs into a discarded scratch directory,
    and runs the same E5/E15/E6 validation ``canonic pack add`` does — everything except
    the connection-dependent ``required_tables`` existence check (§2.4), which needs a
    live database and is out of scope here on purpose.
    """
    repo_dir = resolve_repo(repo, project_root=Path.cwd()) if repo is not None else path
    targets = _resolve_validate_targets(repo_dir)

    json_output = get_cli_context(ctx).json_output
    reports: list[dict[str, Any]] = []
    any_failed = False
    for pack_dir, manifest in targets:
        variants = [manifest.variant(variant_id)] if variant_id is not None else manifest.variants
        for variant in variants:
            result = validate_pack(pack_dir, manifest, variant)
            ok = not result.validation_errors
            any_failed = any_failed or not ok
            reports.append(
                {
                    "pack": manifest.pack,
                    "variant": variant.id,
                    "ok": ok,
                    "files": len(result.written),
                    "errors": result.validation_errors,
                }
            )
            if json_output:
                continue
            if ok:
                _console.print(
                    f"[green]✓[/green] {manifest.pack} / {variant.id} "
                    f"— {len(result.written)} file(s) validated"
                )
            else:
                _console.print(f"[red]✗[/red] {manifest.pack} / {variant.id}")
                for msg in result.validation_errors:
                    _console.print(f"  {msg}")

    if json_output:
        typer.echo(jsonlib.dumps({"results": reports}))
    if any_failed:
        raise typer.Exit(1)


def _resolve_validate_targets(path: Path) -> list[tuple[Path, PackManifest]]:
    if (path / "pack.yaml").exists():
        return [(path, load_pack_manifest(path))]
    if (path / "packs").is_dir():
        return [(find_pack_dir(path, m.pack), m) for m in list_packs(path)]
    raise PackError(f"{path}: not a pack directory (pack.yaml) or a repo root (packs/)")


def _pick_variant(manifest: PackManifest, variant_id: str | None) -> Variant:
    if variant_id is not None:
        return manifest.variant(variant_id)
    if len(manifest.variants) == 1:
        return manifest.variants[0]
    _console.print("multiple variants available:")
    for i, v in enumerate(manifest.variants, 1):
        _console.print(f"  [{i}] {v.id} — {v.label}")
    choice = typer.prompt("Select variant", default="1")
    try:
        return manifest.variants[int(choice) - 1]
    except (ValueError, IndexError) as exc:
        raise PackError(f"invalid variant selection {choice!r}") from exc


def _pick_connection(config: CanonicConfig, variant: Variant, *, non_interactive: bool) -> str:
    candidates = [
        c for c in config.connections if variant.connector is None or c.type == variant.connector
    ]
    if len(candidates) == 1:
        return candidates[0].id
    wanted = variant.connector or "matching"
    if not candidates:
        raise PackError(
            f"no {wanted} connection configured; add one first via "
            f"`canonic connection add --type {wanted}` or `canonic setup`"
        )
    if non_interactive:
        ids = ", ".join(c.id for c in candidates)
        raise PackError(
            f"multiple {wanted} connections found ({ids}); pass --connection or "
            "--param connection_id=<id> to disambiguate"
        )
    _console.print(f"multiple {wanted} connections found:")
    for i, c in enumerate(candidates, 1):
        _console.print(f"  [{i}] {c.id}")
    choice = typer.prompt("Select connection", default="1")
    try:
        return candidates[int(choice) - 1].id
    except (ValueError, IndexError) as exc:
        raise PackError(f"invalid connection selection {choice!r}") from exc


def _explicit_params(param: list[str] | None, params_file: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if params_file is not None:
        values.update(_load_params_file(params_file))
    for kv in param or []:
        if "=" not in kv:
            raise PackError(f"--param must be KEY=VALUE, got {kv!r}")
        k, _, v = kv.partition("=")
        values[k] = v
    return values


def _load_params_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise PackError(f"params file not found: {path}")
    text = path.read_text()
    data: Any = YAML().load(text) if path.suffix in (".yaml", ".yml") else jsonlib.loads(text)
    if not isinstance(data, dict):
        raise PackError(f"{path}: params file must be a mapping of param name to value")
    return {str(k): str(v) for k, v in (data or {}).items()}


def _render_preview(manifest: PackManifest, variant: Variant, params: dict[str, str]) -> None:
    _console.print(f"\n[bold]{manifest.pack} / {variant.id}[/bold] — files to be written:")
    for rel in (
        *manifest.provides.semantics,
        *manifest.provides.contracts.metrics,
        *manifest.provides.contracts.guardrails,
        *manifest.provides.knowledge,
    ):
        _console.print(f"  {rel}")
    for p in manifest.params:
        if p.derive is not None and p.name in params:
            _console.print(f"[dim]derived {p.name} = {params[p.name]!r}[/dim]")
