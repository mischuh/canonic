"""Shared ``--tenant`` CLI flag handling (SPEC-E12 §5, §7).

``--tenant`` exists for local development and the platform-operator path. Per §7, it
is "refused unless the caller is on stdio or holds a tenancy_exempt role, and it warns
on every call." Every direct CLI invocation of ``query``/``sql``/``report run`` already
runs at the stdio-equivalent trust level — no network transport, full local access — so
that fail-closed condition is trivially satisfied for them: the flag is always accepted
and always warns. ``canonic mcp start`` is the one caller where the distinction between
``stdio`` and ``http`` is real; its refusal logic lives in
``canonic.cli.commands.mcp`` alongside the transport it gates.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from canonic.contracts.principal import Principal, normalize_tenants

_console = Console()

#: Reusable annotated CLI option for ``--tenant`` — shared verbatim across the
#: commands SPEC-E12 §7 names (``query``, ``sql``, ``report run``, ``mcp start``). Repeat the
#: flag or comma-separate the values to scope to several tenants.
TenantOption = Annotated[
    list[str] | None,
    typer.Option(
        "--tenant",
        help=(
            "Override the principal's tenant for local development / platform-operator "
            "use (SPEC-E12 §5, §7). Repeat the flag or comma-separate values for several "
            "tenants. Always logs a warning."
        ),
    ),
]


def cli_tenant_principal(tenant: list[str] | None) -> Principal | None:
    """Build the CLI-supplied override Principal for ``tenant``, warning when used.

    ``None`` when ``--tenant`` was not given. Each occurrence of the flag may itself be a
    comma-separated list. The returned :class:`Principal` carries no roles — CLI-direct
    invocation has no verified role claim to bind, unlike an MCP request's
    :class:`~fastmcp.server.auth.auth.AccessToken`. A flag that names no usable value
    (``--tenant ""``) yields a principal with no tenant, which the compiler refuses with
    ``tenant_unresolved``.
    """
    if not tenant:
        return None
    parts = [part.strip() for raw in tenant for part in raw.split(",")]
    tenants = normalize_tenants(parts)
    shown = ", ".join(str(t) for t in tenants) if tenants else "<none>"
    _console.print(
        f"[yellow]warning:[/yellow] --tenant={shown!r} overrides the caller's principal — "
        "local development / platform-operator use only, never for a network-reachable "
        "multi-tenant deployment"
    )
    return Principal(tenants=tenants, roles=(), source="cli-override")
