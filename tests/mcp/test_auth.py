"""Tests for MCP bearer-token and OAuth 2.1 auth (canonic/mcp/auth.py,
AMENDMENT-remote-mcp-transport, AMENDMENT-oauth-mcp-auth).
"""

from __future__ import annotations

import pytest
from fastmcp.server.auth.auth import AccessToken, AuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

from canonic.config import McpAuthConfig, McpOAuthConfig, McpOAuthMode, McpTokenEntry
from canonic.contracts.models import RolePolicy, TenancyPolicy
from canonic.exc import CredentialError
from canonic.mcp.auth import (
    CLI_OVERRIDE_CLIENT_ID,
    CanonicCompositeVerifier,
    CanonicTokenVerifier,
    ResolvedToken,
    build_mcp_auth,
    build_oauth_verifier,
    build_token_verifier,
    describe_auth_mechanisms,
    principal_from_token,
    resolve_tokens,
)


@pytest.fixture
def auth_config(monkeypatch: pytest.MonkeyPatch) -> McpAuthConfig:
    monkeypatch.setenv("CANONIC_TEST_TOKEN_ALICE", "alice-secret")
    monkeypatch.setenv("CANONIC_TEST_TOKEN_BOB", "bob-secret")
    return McpAuthConfig(
        tokens=[
            McpTokenEntry(client_id="alice", token_ref="env:CANONIC_TEST_TOKEN_ALICE"),
            McpTokenEntry(client_id="bob", token_ref="env:CANONIC_TEST_TOKEN_BOB"),
        ]
    )


class TestResolveTokens:
    def test_resolves_each_entry(self, auth_config: McpAuthConfig) -> None:
        tokens = resolve_tokens(auth_config)
        assert tokens == {
            "alice-secret": ResolvedToken(client_id="alice"),
            "bob-secret": ResolvedToken(client_id="bob"),
        }

    def test_resolves_inline_claims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Static tokens carry claims inline (SPEC-E12 §7) — no IdP to ask."""
        monkeypatch.setenv("CANONIC_TEST_TOKEN_MERCHANT", "merchant-secret")
        config = McpAuthConfig(
            tokens=[
                McpTokenEntry(
                    client_id="merchant-4711-agent",
                    token_ref="env:CANONIC_TEST_TOKEN_MERCHANT",
                    claims={"merchant_id": "4711", "roles": ["merchant_viewer"]},
                )
            ]
        )
        tokens = resolve_tokens(config)
        assert tokens["merchant-secret"] == ResolvedToken(
            client_id="merchant-4711-agent",
            claims={"merchant_id": "4711", "roles": ["merchant_viewer"]},
        )

    def test_empty_config_resolves_empty(self) -> None:
        assert resolve_tokens(McpAuthConfig()) == {}

    def test_extra_token_ref_folded_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_CLI_TOKEN", "cli-secret")
        tokens = resolve_tokens(McpAuthConfig(), extra_token_ref="env:CANONIC_TEST_CLI_TOKEN")
        assert tokens == {"cli-secret": ResolvedToken(client_id=CLI_OVERRIDE_CLIENT_ID)}

    def test_unresolvable_ref_raises_credential_error(self) -> None:
        config = McpAuthConfig(
            tokens=[McpTokenEntry(client_id="ghost", token_ref="env:CANONIC_TEST_UNSET_VAR")]
        )
        with pytest.raises(CredentialError):
            resolve_tokens(config)


class TestCanonicTokenVerifier:
    @pytest.mark.asyncio
    async def test_accepts_configured_token(self, auth_config: McpAuthConfig) -> None:
        verifier = CanonicTokenVerifier(resolve_tokens(auth_config))
        access = await verifier.verify_token("alice-secret")
        assert access is not None
        assert access.client_id == "alice"

    @pytest.mark.asyncio
    async def test_verified_token_carries_claims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_TOKEN_MERCHANT", "merchant-secret")
        config = McpAuthConfig(
            tokens=[
                McpTokenEntry(
                    client_id="merchant-4711-agent",
                    token_ref="env:CANONIC_TEST_TOKEN_MERCHANT",
                    claims={"merchant_id": "4711"},
                )
            ]
        )
        verifier = CanonicTokenVerifier(resolve_tokens(config))
        access = await verifier.verify_token("merchant-secret")
        assert access is not None
        assert access.claims == {"merchant_id": "4711"}

    @pytest.mark.asyncio
    async def test_rejects_unknown_token(self, auth_config: McpAuthConfig) -> None:
        verifier = CanonicTokenVerifier(resolve_tokens(auth_config))
        assert await verifier.verify_token("not-a-real-token") is None

    @pytest.mark.asyncio
    async def test_rejects_empty_token(self, auth_config: McpAuthConfig) -> None:
        verifier = CanonicTokenVerifier(resolve_tokens(auth_config))
        assert await verifier.verify_token("") is None


class TestBuildTokenVerifier:
    def test_returns_none_when_no_tokens(self) -> None:
        assert build_token_verifier(McpAuthConfig()) is None

    def test_returns_verifier_when_tokens_configured(self, auth_config: McpAuthConfig) -> None:
        verifier = build_token_verifier(auth_config)
        assert verifier is not None
        assert isinstance(verifier, CanonicTokenVerifier)

    def test_extra_token_ref_alone_is_sufficient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_CLI_TOKEN", "cli-secret")
        verifier = build_token_verifier(
            McpAuthConfig(), extra_token_ref="env:CANONIC_TEST_CLI_TOKEN"
        )
        assert verifier is not None


def _stub_discovery(monkeypatch: pytest.MonkeyPatch, **fields: object) -> list[str]:
    """Monkeypatch OIDC discovery to return a fixed document instead of hitting the network.

    Patches the class attribute directly, which also covers ``OIDCProxy.__init__``'s own
    internal call to the same classmethod (both reference the same class object). Returns
    the list of ``config_url`` values discovery was called with, for assertions.
    """
    calls: list[str] = []

    def _fake_get_oidc_configuration(
        config_url: object, *, strict: object = None, timeout_seconds: object = None
    ) -> OIDCConfiguration:
        calls.append(str(config_url))
        return OIDCConfiguration(strict=False, **fields)

    monkeypatch.setattr(
        OIDCConfiguration, "get_oidc_configuration", staticmethod(_fake_get_oidc_configuration)
    )
    return calls


class TestBuildOAuthVerifier:
    def test_jwt_mode_with_explicit_jwks_uri_does_not_discover(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_discovery(monkeypatch)
        config = McpOAuthConfig(
            mode=McpOAuthMode.JWT,
            issuer_url="https://idp.example.com",
            jwks_uri="https://idp.example.com/jwks.json",
            audience="canonic-mcp",
        )
        verifier = build_oauth_verifier(config)
        assert isinstance(verifier, JWTVerifier)
        assert calls == []

    def test_jwt_mode_discovers_jwks_uri_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_discovery(
            monkeypatch,
            issuer="https://idp.example.com",
            jwks_uri="https://idp.example.com/jwks.json",
        )
        config = McpOAuthConfig(mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com")
        verifier = build_oauth_verifier(config)
        assert isinstance(verifier, JWTVerifier)
        assert calls == ["https://idp.example.com/.well-known/openid-configuration"]

    def test_jwt_mode_discovery_failure_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*args: object, **kwargs: object) -> OIDCConfiguration:
            raise RuntimeError("boom")

        monkeypatch.setattr(OIDCConfiguration, "get_oidc_configuration", staticmethod(_raise))
        config = McpOAuthConfig(mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com")
        with pytest.raises(RuntimeError, match="could not discover OIDC configuration"):
            build_oauth_verifier(config)

    def test_proxy_mode_builds_oidc_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_OAUTH_SECRET", "oauth-client-secret")
        calls = _stub_discovery(
            monkeypatch,
            issuer="https://idp.example.com",
            authorization_endpoint="https://idp.example.com/authorize",
            token_endpoint="https://idp.example.com/token",
            jwks_uri="https://idp.example.com/jwks.json",
        )
        config = McpOAuthConfig(
            mode=McpOAuthMode.PROXY,
            issuer_url="https://idp.example.com",
            client_id="canonic-mcp",
            client_secret_ref="env:CANONIC_TEST_OAUTH_SECRET",
            base_url="https://canonic.internal.example.com",
            scopes=["openid", "profile"],
        )
        verifier = build_oauth_verifier(config)
        assert isinstance(verifier, OIDCProxy)
        assert calls == ["https://idp.example.com/.well-known/openid-configuration"]
        # A functioning OAuth server mounts its DCR/authorize/token routes.
        assert len(verifier.get_routes(mcp_path="/mcp")) > 0
        # Default: verifies the upstream access_token, not the id_token.
        assert verifier._uses_alternate_verification() is False

    def test_proxy_mode_verify_id_token_wired_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANONIC_TEST_OAUTH_SECRET", "oauth-client-secret")
        _stub_discovery(
            monkeypatch,
            issuer="https://idp.example.com",
            authorization_endpoint="https://idp.example.com/authorize",
            token_endpoint="https://idp.example.com/token",
            jwks_uri="https://idp.example.com/jwks.json",
        )
        config = McpOAuthConfig(
            mode=McpOAuthMode.PROXY,
            issuer_url="https://idp.example.com",
            client_id="canonic-mcp",
            client_secret_ref="env:CANONIC_TEST_OAUTH_SECRET",
            base_url="https://canonic.internal.example.com",
            verify_id_token=True,
        )
        verifier = build_oauth_verifier(config)
        assert isinstance(verifier, OIDCProxy)
        # id_token verification is on: needed for IdPs with opaque access tokens
        # (Google, GitHub, ...) and for client_id to be a meaningful identity claim.
        assert verifier._uses_alternate_verification() is True


class _RecordingOAuthProvider(AuthProvider):
    """A minimal real ``AuthProvider`` standing in for the OAuth side of S17 tests."""

    def __init__(self) -> None:
        super().__init__()
        self.verify_calls: list[str] = []
        self.set_mcp_path_calls: list[str | None] = []
        self._routes = ["authorize-route", "token-route"]

    async def verify_token(self, token: str) -> AccessToken | None:
        self.verify_calls.append(token)
        if token == "oauth-token":
            return AccessToken(token=token, client_id="oauth-user", scopes=[])
        return None

    def get_routes(self, mcp_path: str | None = None) -> list[str]:
        return self._routes

    def set_mcp_path(self, mcp_path: str | None) -> None:
        self.set_mcp_path_calls.append(mcp_path)


class TestCanonicCompositeVerifier:
    @pytest.fixture
    def composite(
        self, auth_config: McpAuthConfig
    ) -> tuple[CanonicCompositeVerifier, _RecordingOAuthProvider]:
        static = CanonicTokenVerifier(resolve_tokens(auth_config))
        oauth = _RecordingOAuthProvider()
        return CanonicCompositeVerifier(static, oauth), oauth

    @pytest.mark.asyncio
    async def test_static_token_accepted_without_consulting_oauth(
        self,
        composite: tuple[CanonicCompositeVerifier, _RecordingOAuthProvider],
    ) -> None:
        verifier, oauth = composite
        access = await verifier.verify_token("alice-secret")
        assert access is not None
        assert access.client_id == "alice"
        assert oauth.verify_calls == []  # S17 AC1: no network call for a static match

    @pytest.mark.asyncio
    async def test_oauth_token_falls_through_when_no_static_match(
        self,
        composite: tuple[CanonicCompositeVerifier, _RecordingOAuthProvider],
    ) -> None:
        verifier, oauth = composite
        access = await verifier.verify_token("oauth-token")
        assert access is not None
        assert access.client_id == "oauth-user"  # S17 AC2
        assert oauth.verify_calls == ["oauth-token"]

    @pytest.mark.asyncio
    async def test_neither_matches_rejects(
        self,
        composite: tuple[CanonicCompositeVerifier, _RecordingOAuthProvider],
    ) -> None:
        verifier, _ = composite
        assert await verifier.verify_token("not-a-real-token") is None  # S17 AC3

    def test_get_routes_delegates_to_oauth(
        self,
        composite: tuple[CanonicCompositeVerifier, _RecordingOAuthProvider],
    ) -> None:
        verifier, oauth = composite
        assert verifier.get_routes(mcp_path="/mcp") == oauth._routes

    def test_set_mcp_path_delegates_to_oauth(
        self,
        composite: tuple[CanonicCompositeVerifier, _RecordingOAuthProvider],
    ) -> None:
        verifier, oauth = composite
        verifier.set_mcp_path("/mcp")
        assert oauth.set_mcp_path_calls == ["/mcp"]


class TestBuildMcpAuth:
    def test_neither_configured_returns_none(self) -> None:
        assert build_mcp_auth(McpAuthConfig()) is None

    def test_tokens_only_returns_token_verifier(self, auth_config: McpAuthConfig) -> None:
        auth = build_mcp_auth(auth_config)
        assert isinstance(auth, CanonicTokenVerifier)

    def test_oauth_only_returns_oauth_verifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_discovery(
            monkeypatch,
            issuer="https://idp.example.com",
            jwks_uri="https://idp.example.com/jwks.json",
        )
        config = McpAuthConfig(
            oauth=McpOAuthConfig(mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com")
        )
        auth = build_mcp_auth(config)
        assert isinstance(auth, JWTVerifier)

    def test_both_configured_returns_composite(
        self, auth_config: McpAuthConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_discovery(
            monkeypatch,
            issuer="https://idp.example.com",
            jwks_uri="https://idp.example.com/jwks.json",
        )
        auth_config.oauth = McpOAuthConfig(
            mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com"
        )
        auth = build_mcp_auth(auth_config)
        assert isinstance(auth, CanonicCompositeVerifier)


class TestDescribeAuthMechanisms:
    def test_empty_config(self) -> None:
        assert describe_auth_mechanisms(McpAuthConfig()) == []

    def test_tokens_only(self, auth_config: McpAuthConfig) -> None:
        assert describe_auth_mechanisms(auth_config) == ["token"]

    def test_oauth_only(self) -> None:
        config = McpAuthConfig(
            oauth=McpOAuthConfig(mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com")
        )
        assert describe_auth_mechanisms(config) == ["oauth-jwt"]

    def test_both(self, auth_config: McpAuthConfig) -> None:
        auth_config.oauth = McpOAuthConfig(
            mode=McpOAuthMode.JWT, issuer_url="https://idp.example.com"
        )
        assert describe_auth_mechanisms(auth_config) == ["token", "oauth-jwt"]


class TestPrincipalFromToken:
    """``principal_from_token`` (SPEC-E12 §5, §7) — never reads anything but the
    verified token's claims.
    """

    def _token(self, **claims: object) -> AccessToken:
        return AccessToken(token="t", client_id="merchant-4711-agent", scopes=[], claims=claims)

    def test_neither_policy_configured_returns_none(self) -> None:
        assert principal_from_token(self._token(), tenancy=None, roles=None) is None

    def test_tenant_read_from_claim(self) -> None:
        tenancy = TenancyPolicy(
            schema="tenancy/v1", claim="merchant_id", scoped_sources=[], shared_sources=[]
        )
        principal = principal_from_token(
            self._token(merchant_id="4711"), tenancy=tenancy, roles=None
        )
        assert principal is not None
        assert principal.tenant == "4711"
        assert principal.roles == ()

    def test_missing_tenant_claim_yields_none_tenant(self) -> None:
        tenancy = TenancyPolicy(
            schema="tenancy/v1", claim="merchant_id", scoped_sources=[], shared_sources=[]
        )
        principal = principal_from_token(self._token(), tenancy=tenancy, roles=None)
        assert principal is not None
        assert principal.tenant is None

    def test_roles_read_from_list_claim(self) -> None:
        roles = RolePolicy(schema="roles/v1", claim="roles", roles={})
        principal = principal_from_token(
            self._token(roles=["merchant_viewer", "merchant_admin"]), tenancy=None, roles=roles
        )
        assert principal is not None
        assert principal.roles == ("merchant_viewer", "merchant_admin")

    def test_roles_read_from_scalar_claim(self) -> None:
        roles = RolePolicy(schema="roles/v1", claim="role", roles={})
        principal = principal_from_token(
            self._token(role="merchant_viewer"), tenancy=None, roles=roles
        )
        assert principal is not None
        assert principal.roles == ("merchant_viewer",)

    def test_missing_roles_claim_yields_empty_roles(self) -> None:
        roles = RolePolicy(schema="roles/v1", claim="roles", roles={})
        principal = principal_from_token(self._token(), tenancy=None, roles=roles)
        assert principal is not None
        assert principal.roles == ()

    def test_claim_mapping_resolves_namespaced_claim(self) -> None:
        """OAuth claims are often namespaced; static tokens need no mapping (SPEC-E12 §7)."""
        tenancy = TenancyPolicy(
            schema="tenancy/v1", claim="merchant_id", scoped_sources=[], shared_sources=[]
        )
        token = self._token(**{"https://example.com/merchant_id": "4711"})
        principal = principal_from_token(
            token,
            tenancy=tenancy,
            roles=None,
            claim_mapping={"merchant_id": "https://example.com/merchant_id"},
        )
        assert principal is not None
        assert principal.tenant == "4711"

    def test_both_policies_combine(self) -> None:
        tenancy = TenancyPolicy(
            schema="tenancy/v1", claim="merchant_id", scoped_sources=[], shared_sources=[]
        )
        roles = RolePolicy(schema="roles/v1", claim="roles", roles={})
        principal = principal_from_token(
            self._token(merchant_id="4711", roles=["merchant_viewer"]), tenancy=tenancy, roles=roles
        )
        assert principal is not None
        assert principal.tenant == "4711"
        assert principal.roles == ("merchant_viewer",)
