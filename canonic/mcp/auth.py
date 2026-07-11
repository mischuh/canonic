"""Bearer-token auth for the MCP daemon's ``http`` transport (AMENDMENT-remote-mcp-transport).

``stdio`` transport keeps its current no-auth model (process-level trust is sufficient
for a local subprocess) and never touches this module. ``http`` transport is
network-reachable once bound, so it requires at least one resolvable token before the
daemon is allowed to start — see :func:`build_token_verifier` and its caller in
``canonic.mcp.daemon.start_http``.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import AccessToken, TokenVerifier

from canonic.credentials import resolve_credential

if TYPE_CHECKING:
    from canonic.config import McpAuthConfig

__all__ = ["CanonicTokenVerifier", "build_token_verifier", "resolve_tokens"]

#: client_id assigned to a token supplied via the ``--token-ref`` CLI override
#: rather than a named entry in ``mcp.auth.tokens``.
CLI_OVERRIDE_CLIENT_ID = "cli-override"


def resolve_tokens(
    auth_config: McpAuthConfig, *, extra_token_ref: str | None = None
) -> dict[str, str]:
    """Resolve every configured ``token_ref`` into ``{secret_token: client_id}``.

    Folds in ``extra_token_ref`` (the ``--token-ref`` CLI override, attributed to
    :data:`CLI_OVERRIDE_CLIENT_ID`) when given. Raises
    :class:`canonic.exc.CredentialError` if any reference cannot be resolved.
    """
    tokens = {resolve_credential(entry.token_ref): entry.client_id for entry in auth_config.tokens}
    if extra_token_ref is not None:
        tokens[resolve_credential(extra_token_ref)] = CLI_OVERRIDE_CLIENT_ID
    return tokens


class CanonicTokenVerifier(TokenVerifier):
    """Verifies a bearer token against a fixed ``{token: client_id}`` map.

    Deliberately not FastMCP's own ``StaticTokenVerifier`` — that class's docstring
    warns it is for testing/development only. This verifier resolves its tokens from
    ``*_ref`` indirection (never a literal secret in ``canonic.yaml``) and compares in
    constant time.
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        super().__init__()
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        for candidate, client_id in self._tokens.items():
            if hmac.compare_digest(candidate, token):
                return AccessToken(token=token, client_id=client_id, scopes=[])
        return None


def build_token_verifier(
    auth_config: McpAuthConfig, *, extra_token_ref: str | None = None
) -> CanonicTokenVerifier | None:
    """Build a :class:`CanonicTokenVerifier` from config, or ``None`` if no tokens resolve.

    Callers starting a network-reachable transport (``http``) must treat ``None`` as a
    hard error — see ``canonic.mcp.daemon.start_http``.
    """
    tokens = resolve_tokens(auth_config, extra_token_ref=extra_token_ref)
    if not tokens:
        return None
    return CanonicTokenVerifier(tokens)
