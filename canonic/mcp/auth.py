"""Bearer-token and OAuth 2.1 auth for the MCP daemon's ``http`` transport
(AMENDMENT-remote-mcp-transport, AMENDMENT-oauth-mcp-auth).

``stdio`` transport keeps its current no-auth model (process-level trust is sufficient
for a local subprocess) and never touches this module. ``http`` transport is
network-reachable once bound, so it requires at least one resolvable mechanism (static
token and/or OAuth) before the daemon is allowed to start — see :func:`build_mcp_auth`
and its caller in ``canonic.cli.commands.mcp``.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from canonic.config import McpOAuthMode
from canonic.credentials import resolve_credential

if TYPE_CHECKING:
    from starlette.routing import Route

    from canonic.config import McpAuthConfig, McpOAuthConfig

__all__ = [
    "CLI_OVERRIDE_CLIENT_ID",
    "CanonicCompositeVerifier",
    "CanonicTokenVerifier",
    "build_mcp_auth",
    "build_oauth_verifier",
    "build_token_verifier",
    "describe_auth_mechanisms",
    "resolve_tokens",
]

#: Discovery-document suffix appended to ``issuer_url`` for OIDC discovery.
_OIDC_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"

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


def _discover_jwks_uri(issuer_url: str) -> str:
    """Fetch ``jwks_uri`` from the IdP's OIDC discovery document.

    Raises :class:`RuntimeError` naming ``issuer_url`` on any discovery failure
    (network error, non-2xx response, malformed document) — the CLI's ``mcp start``
    already turns a ``RuntimeError`` into a clean ``error:`` line and exit 1.
    """
    discovery_url = issuer_url.rstrip("/") + _OIDC_DISCOVERY_SUFFIX
    try:
        config = OIDCConfiguration.get_oidc_configuration(
            AnyHttpUrl(discovery_url), strict=None, timeout_seconds=10
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not discover OIDC configuration at {discovery_url}: {exc}"
        ) from exc
    if config.jwks_uri is None:
        raise RuntimeError(f"OIDC configuration at {discovery_url} has no jwks_uri")
    return str(config.jwks_uri)


def build_oauth_verifier(oauth_config: McpOAuthConfig) -> AuthProvider:
    """Build the OAuth 2.1 :class:`AuthProvider` fastmcp's ``mode`` config selects.

    - ``jwt``: :class:`JWTVerifier`, verifying a client-presented JWT's signature
      against the IdP's JWKS (``jwks_uri`` if configured, else discovered from
      ``issuer_url``). No proxy state, no redirect handling.
    - ``proxy``: :class:`OIDCProxy`, presenting a DCR-compliant OAuth server to MCP
      clients and relaying the actual login to the configured upstream IdP
      (Authorization Code + PKCE). ``verify_id_token`` controls whether the upstream
      *access* token (default) or *id_token* is what gets verified — see
      :attr:`McpOAuthConfig.verify_id_token` for why an IdP with opaque access tokens
      needs the latter.

    Raises :class:`canonic.exc.CredentialError` if ``client_secret_ref`` cannot be
    resolved, or :class:`RuntimeError` if IdP discovery fails.
    """
    if oauth_config.mode == McpOAuthMode.JWT:
        jwks_uri = oauth_config.jwks_uri or _discover_jwks_uri(oauth_config.issuer_url)
        return JWTVerifier(
            jwks_uri=jwks_uri,
            issuer=oauth_config.issuer_url,
            audience=oauth_config.audience,
            required_scopes=oauth_config.scopes or None,
        )

    discovery_url = oauth_config.issuer_url.rstrip("/") + _OIDC_DISCOVERY_SUFFIX
    client_secret = (
        resolve_credential(oauth_config.client_secret_ref)
        if oauth_config.client_secret_ref is not None
        else None
    )
    assert oauth_config.client_id is not None  # enforced by McpOAuthConfig validation
    assert oauth_config.base_url is not None  # enforced by McpOAuthConfig validation
    return OIDCProxy(
        config_url=discovery_url,
        client_id=oauth_config.client_id,
        client_secret=client_secret,
        base_url=oauth_config.base_url,
        required_scopes=oauth_config.scopes or None,
        verify_id_token=oauth_config.verify_id_token,
    )


class CanonicCompositeVerifier(AuthProvider):
    """Composes static bearer-token auth with OAuth 2.1 auth (S17, AMENDMENT-oauth-mcp-auth).

    Tries the static token map first (fast, no network call), falling through to the
    OAuth verifier's ``verify_token`` if no static token matches. The two stay
    independently revocable: a token entry is revoked by editing ``canonic.yaml``, an
    OAuth-issued token is revoked at the IdP.

    Route/path setup (``get_routes``, ``set_mcp_path``) delegates to the wrapped OAuth
    provider, since ``OAuthProxy``/``OIDCProxy`` mount the DCR/authorize/token/callback
    endpoints there — a plain ``TokenVerifier`` wrapper would silently drop them.
    ``get_middleware`` is deliberately **not** overridden: the inherited
    ``AuthProvider.get_middleware`` binds ``BearerAuthBackend(self)``, routing every
    request's bearer token through this class's own ``verify_token`` (static-first,
    OAuth-fallback) rather than the OAuth provider's alone.
    """

    def __init__(self, static: CanonicTokenVerifier, oauth: AuthProvider) -> None:
        super().__init__(
            base_url=oauth.base_url,
            resource_base_url=oauth.resource_base_url,
            required_scopes=oauth.required_scopes,
        )
        self._static = static
        self._oauth = oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await self._static.verify_token(token)
        if access is not None:
            return access
        return await self._oauth.verify_token(token)

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        return self._oauth.get_routes(mcp_path)

    def set_mcp_path(self, mcp_path: str | None) -> None:
        super().set_mcp_path(mcp_path)
        self._oauth.set_mcp_path(mcp_path)


def build_mcp_auth(
    auth_config: McpAuthConfig, *, extra_token_ref: str | None = None
) -> AuthProvider | None:
    """Build the ``http`` transport's auth provider from ``mcp.auth`` config.

    The single entry point ``canonic mcp start`` calls. ``tokens`` and ``oauth`` are
    independently optional and compose (S17):

    - both resolve -> :class:`CanonicCompositeVerifier` (static tokens checked first)
    - only one resolves -> that verifier alone
    - neither -> ``None`` (callers starting ``http`` transport must treat this as a
      hard error, exactly as before this amendment)
    """
    token_verifier = build_token_verifier(auth_config, extra_token_ref=extra_token_ref)
    oauth_verifier = (
        build_oauth_verifier(auth_config.oauth) if auth_config.oauth is not None else None
    )

    if token_verifier is not None and oauth_verifier is not None:
        return CanonicCompositeVerifier(token_verifier, oauth_verifier)
    if token_verifier is not None:
        return token_verifier
    if oauth_verifier is not None:
        return oauth_verifier
    return None


def describe_auth_mechanisms(
    auth_config: McpAuthConfig, *, extra_token_ref: str | None = None
) -> list[str]:
    """Human-readable list of auth mechanisms that :func:`build_mcp_auth` would enable.

    Used to populate ``DaemonState.auth_mechanisms`` (``canonic mcp status``) without
    re-deriving the verifier logic — e.g. ``["token", "oauth-proxy"]``.
    """
    mechanisms: list[str] = []
    if resolve_tokens(auth_config, extra_token_ref=extra_token_ref):
        mechanisms.append("token")
    if auth_config.oauth is not None:
        mechanisms.append(f"oauth-{auth_config.oauth.mode.value}")
    return mechanisms
