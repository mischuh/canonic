"""``app.getcanonic/contract`` MCP server extension — advertises ``contract_schema``.

The sessionless era (SEP-2133 / ``2026-07-28``) has no per-session handshake, so the
two-tool version check (``contract_info`` / ``negotiate_contract``, still served as a
fallback for clients without extension support) cannot gate anything before a tool
runs. This extension advertises ``contract_schema`` under
``capabilities.extensions["app.getcanonic/contract"]`` on both eras, and — for a
client that opts in for a given request — rejects a MAJOR mismatch or a MINOR the
server does not yet implement before the tool body executes (AMENDMENT-fastmcp4-
adoption §2, S19).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.server.extensions import ServerExtension
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError

from canonic.contract import CONTRACT_SCHEMA

if TYPE_CHECKING:
    from fastmcp.server.context import Context
    from fastmcp.server.extensions import ToolCallContinuation, ToolCallOutcome

__all__ = ["ContractExtension"]

_MAJOR, _MINOR = (int(part) for part in CONTRACT_SCHEMA.split("."))


class ContractExtension(ServerExtension):
    """Advertises ``contract_schema`` and enforces it for clients that opt in."""

    identifier = "app.getcanonic/contract"

    def settings(self) -> dict[str, Any]:
        return {"schema": CONTRACT_SCHEMA, "major": _MAJOR, "minor": _MINOR}

    async def intercept_tool_call(
        self,
        params: mcp_types.CallToolRequestParams,
        context: Context,
        call_next: ToolCallContinuation,
    ) -> ToolCallOutcome:
        declared = context.client_extension_settings(self.identifier)
        if declared is None:
            return await call_next()

        client_major = declared.get("major")
        client_minor = declared.get("minor", 0)
        if client_major != _MAJOR or (isinstance(client_minor, int) and client_minor > _MINOR):
            raise MCPError(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"contract_schema mismatch: client declared {client_major}.{client_minor}, "
                    f"server implements {CONTRACT_SCHEMA}."
                ),
            )
        return await call_next()
