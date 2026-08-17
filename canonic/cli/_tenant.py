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

from canonic.contracts.principal import Principal

_console = Console()

#: Reusable annotated CLI option for ``--tenant`` — shared verbatim across the
#: commands SPEC-E12 §7 names (``query``, ``sql``, ``report run``, ``mcp start``).
TenantOption = Annotated[
    str | None,
    typer.Option(
        "--tenant",
        help=(
            "Override the principal's tenant for local development / platform-operator "
            "use (SPEC-E12 §5, §7). Always logs a warning."
        ),
    ),
]


def cli_tenant_principal(tenant: str | None) -> Principal | None:
    """Build the CLI-supplied override Principal for ``tenant``, warning when used.

    ``None`` when ``--tenant`` was not given. The returned :class:`Principal` carries
    no roles — CLI-direct invocation has no verified role claim to bind, unlike an MCP
    request's :class:`~fastmcp.server.auth.auth.AccessToken`.
    """
    if tenant is None:
        return None
    _console.print(
        f"[yellow]warning:[/yellow] --tenant={tenant!r} overrides the caller's principal — "
        "local development / platform-operator use only, never for a network-reachable "
        "multi-tenant deployment"
    )
    return Principal(tenant=tenant, roles=(), source="cli-override")
