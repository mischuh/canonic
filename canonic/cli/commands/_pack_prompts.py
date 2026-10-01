"""Shared interactive param-resolution prompts for context packs (§5.4, §5.5).

Used by both ``canonic pack add`` (``pack.py``) and the ``canonic setup`` wizard's pack
branch (``setup.py``) — two entry points into the same prompt flow (AMENDMENT-context-
packs §5), so choose_from/derive/validation behavior never drifts between them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from canonic.core.service import CanonicService
from canonic.exc import CanonicError
from canonic.packs.install import resolve_params
from canonic.packs.templating import substitute

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from canonic.packs.install import InstallResult
    from canonic.packs.manifest import FirstAnswer, PackManifest, Param

_console = Console()

__all__ = [
    "prompt_choose_from",
    "prompt_plain",
    "render_install_result",
    "resolve_params_interactively",
    "run_and_render_first_answer",
    "run_choose_from_query",
    "seed_defaults",
]


def seed_defaults(manifest: PackManifest, explicit: dict[str, str]) -> dict[str, str]:
    """Every param with a default, filled in unless already set — enough to evaluate
    ``required_tables`` before any live query runs."""
    seeded = dict(explicit)
    for p in manifest.params:
        if p.name not in seeded and p.default is not None:
            seeded[p.name] = p.default
    return seeded


def prompt_plain(p: Param) -> str:
    """A free-text prompt for a param with no ``choose_from``/``derive``."""
    text = p.description or p.name
    if p.default is not None:
        return str(typer.prompt(text, default=p.default))
    return str(typer.prompt(text))


def run_choose_from_query(
    root: Path, query: str | None, seeded: dict[str, str], connection_id: str
) -> list[tuple[str, int]]:
    """§5.4.1: run a pack's ``choose_from.query`` through the real ``canonic sql`` path.

    Already bounded (statement timeout + row limit, connector-level) and read-only
    enforced — no new execution machinery. Any failure degrades to an empty result
    (the caller falls back to free text) rather than crashing the flow.
    """
    if not query:
        return []
    sql = substitute(query, seeded, source="pack.yaml#choose_from")
    try:
        service = CanonicService.from_project(root)
        result = asyncio.run(service.run_sql(sql, connection_id))
    except Exception as exc:  # noqa: BLE001 — choose_from degrades to free text, never crashes
        _console.print(
            f"[yellow]choose_from query failed, falling back to free text:[/yellow] {exc}"
        )
        return []
    return [(str(row[0]), int(row[1]) if len(row) > 1 else 0) for row in result.rows]


def prompt_choose_from(
    root: Path,
    p: Param,
    manifest: PackManifest,
    seeded: dict[str, str],
    connection_id: str,
    cache: dict[str, list[tuple[str, int]]],
) -> str:
    """§5.4.1: present the query's rows as a numbered pick list; ``allow_custom`` permits
    typing a value not in the list. ``same_as`` reuses another param's already-run query."""
    cf = p.choose_from
    assert cf is not None  # guarded by caller
    cache_key = cf.same_as or p.name
    rows = cache.get(cache_key)
    if rows is None:
        query_owner = manifest.param(cf.same_as) if cf.same_as is not None else p
        query = query_owner.choose_from.query if query_owner and query_owner.choose_from else None
        rows = run_choose_from_query(root, query, seeded, connection_id)
        cache[cache_key] = rows

    if not rows:
        _console.print(f"[yellow]no candidates found for {p.name}[/yellow] — enter one manually")
        return str(typer.prompt(p.description or p.name))

    _console.print(f"\n{p.description or p.name}:")
    for i, (value, n) in enumerate(rows, 1):
        _console.print(f"  [{i}] {value}  (n={n})")
    prompt_text = "Select" + (" (or type a custom value)" if cf.allow_custom else "")
    while True:
        choice = str(typer.prompt(prompt_text, default="1"))
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1][0]
        if cf.allow_custom:
            return choice
        _console.print(f"[red]enter a number 1-{len(rows)}[/red]")


def resolve_params_interactively(
    root: Path,
    manifest: PackManifest,
    explicit: dict[str, str],
    connection_id: str,
    *,
    check_tables: Callable[[dict[str, str]], None],
) -> dict[str, str]:
    """§5.5 step 4: plain questions first, then ``required_tables`` (via ``check_tables``),
    then ``choose_from`` lists (only run once that check has passed, §5.4.1), then
    ``derive`` and the final missing-required check (:func:`canonic.packs.install.resolve_params`).
    """
    for p in manifest.params:
        if p.name in explicit or p.derive is not None or p.choose_from is not None:
            continue
        explicit[p.name] = prompt_plain(p)

    seeded = seed_defaults(manifest, explicit)
    check_tables(seeded)

    cache: dict[str, list[tuple[str, int]]] = {}
    for p in manifest.params:
        if p.name in explicit or p.choose_from is None:
            continue
        explicit[p.name] = prompt_choose_from(root, p, manifest, seeded, connection_id, cache)

    return resolve_params(manifest, explicit)


def render_install_result(result: InstallResult) -> None:
    """§5.5 step 6 output: what got written, and any post-write validation failure."""
    _console.print(
        f"\n[green]✓[/green] installed {result.pack} {result.version} ({result.variant})"
    )
    for path in result.written:
        _console.print(f"  wrote {path}")
    if result.validation_errors:
        _console.print("[red]validation failed:[/red]")
        for msg in result.validation_errors:
            _console.print(f"  {msg}")


def run_and_render_first_answer(root: Path, spec: FirstAnswer) -> bool:
    """§5.6: run and render the pack's first-answer query.

    Returns True on success. On failure the underlying error is printed and installed
    files are left in place (§5.6) — the caller decides whether to exit non-zero.
    """
    from canonic.packs.first_answer import run_first_answer

    _console.print("\n[dim]running first answer…[/dim]")
    try:
        outcome = run_first_answer(root, spec)
    except CanonicError as exc:
        code = exc.code.value if exc.code is not None else "internal_error"
        _console.print(f"[red]first answer failed[/red] [bold]{code}[/bold]: {exc}")
        return False

    rs = outcome.result.result
    table = Table(
        show_header=True, header_style="bold cyan", title=f"first answer: {outcome.source_name}"
    )
    for col in rs.columns:
        table.add_column(col.name)
    for row in rs.rows:
        table.add_row(*(str(v) for v in row))
    _console.print(table)
    if rs.truncated:
        _console.print("[yellow]note:[/yellow] result truncated at the connection row limit")
    return True
