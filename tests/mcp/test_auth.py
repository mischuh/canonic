"""Tests for MCP bearer-token auth (canonic/mcp/auth.py, AMENDMENT-remote-mcp-transport)."""

from __future__ import annotations

import pytest

from canonic.config import McpAuthConfig, McpTokenEntry
from canonic.exc import CredentialError
from canonic.mcp.auth import (
    CLI_OVERRIDE_CLIENT_ID,
    CanonicTokenVerifier,
    build_token_verifier,
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
        assert tokens == {"alice-secret": "alice", "bob-secret": "bob"}

    def test_empty_config_resolves_empty(self) -> None:
        assert resolve_tokens(McpAuthConfig()) == {}

    def test_extra_token_ref_folded_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_CLI_TOKEN", "cli-secret")
        tokens = resolve_tokens(McpAuthConfig(), extra_token_ref="env:CANONIC_TEST_CLI_TOKEN")
        assert tokens == {"cli-secret": CLI_OVERRIDE_CLIENT_ID}

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
