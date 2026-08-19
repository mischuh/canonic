"""Tests for canonic/credentials.py — credentials_ref resolution (E1 §3/§7, #65).

Covers both credential shapes: the static ``env:``/``keyring:``/``file:`` schemes and
the dynamic ``provider:`` scheme with its central caching/refresh layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from canonic.credentials import (
    CachingCredentialProvider,
    CredentialProviderRegistry,
    ResolvedCredential,
    credential_source,
    default_provider_registry,
    resolve_credential,
)
from canonic.exc import CredentialError, UnknownCredentialProvider

if TYPE_CHECKING:
    from collections.abc import Mapping


def test_env_ref_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANONIC_TEST_SECRET", "s3cr3t")
    assert resolve_credential("env:CANONIC_TEST_SECRET") == "s3cr3t"


def test_env_ref_missing_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANONIC_TEST_SECRET", raising=False)
    with pytest.raises(CredentialError, match="CANONIC_TEST_SECRET"):
        resolve_credential("env:CANONIC_TEST_SECRET")


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_env_ref_empty_value_resolves_to_nothing(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An env var that exists but holds an empty/whitespace value "resolves to nothing"
    # and fails with a clear, value-free error (#65).
    monkeypatch.setenv("CANONIC_TEST_SECRET", value)
    with pytest.raises(CredentialError, match="CANONIC_TEST_SECRET"):
        resolve_credential("env:CANONIC_TEST_SECRET")


def test_env_ref_missing_var_name_raises() -> None:
    with pytest.raises(CredentialError):
        resolve_credential("env:")


def test_malformed_ref_raises() -> None:
    with pytest.raises(CredentialError, match="malformed"):
        resolve_credential("CANONIC_TEST_SECRET")


@pytest.mark.parametrize("scheme", ["file", "keyring"])
def test_unimplemented_schemes_raise(scheme: str) -> None:
    with pytest.raises(CredentialError, match="not yet supported"):
        resolve_credential(f"{scheme}:something")


def test_unknown_scheme_raises() -> None:
    with pytest.raises(CredentialError, match="unknown"):
        resolve_credential("vault:secret/x")


def test_provider_ref_rejected_by_resolve_credential() -> None:
    # A provider credential expires, so a caller that freezes one string for the life of
    # the process must not get one — it would silently keep using a dead credential.
    with pytest.raises(CredentialError, match="dynamic credential provider"):
        resolve_credential("provider:aws-iam-redshift")


class _FakeProvider:
    """Counts fetches and hands out a credential with a caller-controlled expiry."""

    def __init__(self, *, ttl: timedelta | None = timedelta(minutes=15)) -> None:
        self.calls = 0
        self._ttl = ttl
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def get(self) -> ResolvedCredential:
        self.calls += 1
        return ResolvedCredential(
            value=f"token-{self.calls}",
            expires_at=None if self._ttl is None else self.now + self._ttl,
            username=f"IAM:user-{self.calls}",
        )


class _FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get(self) -> ResolvedCredential:
        self.calls += 1
        raise CredentialError("GetClusterCredentials denied")


class TestResolvedCredential:
    def test_no_expiry_is_always_fresh(self) -> None:
        cred = ResolvedCredential(value="s3cr3t")
        assert cred.is_fresh(now=datetime(2099, 1, 1, tzinfo=UTC))

    def test_expiry_beyond_skew_is_fresh(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        cred = ResolvedCredential(value="s3cr3t", expires_at=now + timedelta(minutes=15))
        assert cred.is_fresh(now=now)

    def test_expiry_inside_skew_is_stale(self) -> None:
        # Still technically valid, but not for long enough to survive a connect.
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        cred = ResolvedCredential(value="s3cr3t", expires_at=now + timedelta(seconds=30))
        assert not cred.is_fresh(now=now)


class TestCachingCredentialProvider:
    def test_second_read_inside_ttl_reuses_the_cached_value(self) -> None:
        # S2/AC1: two connects within the credential's TTL cost one provider call.
        provider = _FakeProvider()
        source = CachingCredentialProvider(provider, clock=lambda: provider.now)

        first = source.fetch_if_stale()
        second = source.fetch_if_stale()

        assert provider.calls == 1
        assert first is second
        assert second.value == "token-1"

    def test_read_after_ttl_fetches_a_fresh_credential(self) -> None:
        # S1/AC1: past the token's lifetime the next read is a new credential.
        provider = _FakeProvider(ttl=timedelta(minutes=15))
        source = CachingCredentialProvider(provider, clock=lambda: provider.now)

        assert source.fetch_if_stale().value == "token-1"
        provider.now += timedelta(minutes=20)
        assert source.fetch_if_stale().value == "token-2"
        assert provider.calls == 2

    def test_credential_without_expiry_is_fetched_once(self) -> None:
        provider = _FakeProvider(ttl=None)
        source = CachingCredentialProvider(provider, clock=lambda: provider.now)

        source.fetch_if_stale()
        provider.now += timedelta(days=365)
        source.fetch_if_stale()

        assert provider.calls == 1

    def test_fetch_failure_propagates(self) -> None:
        # S4/AC1: the provider's error surfaces; nothing is served in its place.
        source = CachingCredentialProvider(_FailingProvider())
        with pytest.raises(CredentialError, match="denied"):
            source.fetch_if_stale()

    def test_failed_refresh_does_not_serve_the_expired_value(self) -> None:
        # S4/AC1: an expired credential is never silently retried after a failed refresh.
        class _OnceThenFail:
            def __init__(self) -> None:
                self.calls = 0
                self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

            def get(self) -> ResolvedCredential:
                self.calls += 1
                if self.calls > 1:
                    raise CredentialError("token endpoint unavailable")
                return ResolvedCredential(
                    value="token-1", expires_at=self.now + timedelta(minutes=15)
                )

        provider = _OnceThenFail()
        source = CachingCredentialProvider(provider, clock=lambda: provider.now)
        assert source.fetch_if_stale().value == "token-1"

        provider.now += timedelta(minutes=20)
        with pytest.raises(CredentialError, match="unavailable"):
            source.fetch_if_stale()

    def test_empty_provider_value_is_rejected(self) -> None:
        class _EmptyProvider:
            def get(self) -> ResolvedCredential:
                return ResolvedCredential(value="   ")

        source = CachingCredentialProvider(_EmptyProvider())
        with pytest.raises(CredentialError, match="empty value"):
            source.fetch_if_stale()

    def test_cached_value_before_any_fetch_raises(self) -> None:
        source = CachingCredentialProvider(_FakeProvider())
        with pytest.raises(CredentialError, match="refresh"):
            source.cached_value()

    async def test_arefresh_populates_the_cache(self) -> None:
        provider = _FakeProvider()
        source = CachingCredentialProvider(provider, clock=lambda: provider.now)

        await source.arefresh()

        assert source.cached_value().value == "token-1"
        assert source.cached_value().username == "IAM:user-1"


class TestCredentialProviderRegistry:
    def test_registered_provider_is_built_with_connection_params(self) -> None:
        registry = CredentialProviderRegistry()
        seen: dict[str, object] = {}

        def factory(params: Mapping[str, object]) -> _FakeProvider:
            seen.update(params)
            return _FakeProvider()

        registry.register("fake", factory)
        registry.create("fake", {"region": "eu-central-1"})

        assert seen == {"region": "eu-central-1"}

    def test_unknown_provider_lists_registered_names(self) -> None:
        # S3/AC1: no silent fallback, and the error says what *is* available.
        registry = CredentialProviderRegistry()
        registry.register("fake", lambda params: _FakeProvider())

        with pytest.raises(UnknownCredentialProvider) as excinfo:
            registry.create("does-not-exist", {})

        assert excinfo.value.name == "does-not-exist"
        assert "fake" in str(excinfo.value)

    def test_builtin_providers_are_registered_lazily(self) -> None:
        # The default registry loads canonic.connectors.credential_providers on first
        # use, so a bare credential_source() call works without importing connectors.
        assert "aws-iam-redshift" in default_provider_registry.registered_names()


class TestCredentialSourceFactory:
    def test_static_ref_resolves_eagerly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANONIC_TEST_SECRET", "s3cr3t")
        source = credential_source("env:CANONIC_TEST_SECRET")

        assert not source.is_dynamic
        assert source.cached_value().value == "s3cr3t"

    def test_static_ref_failure_surfaces_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CANONIC_TEST_SECRET", raising=False)
        with pytest.raises(CredentialError, match="CANONIC_TEST_SECRET"):
            credential_source("env:CANONIC_TEST_SECRET")

    def test_provider_ref_performs_no_io_at_construction(self) -> None:
        # The first fetch belongs to the first connect; resolving here would produce
        # exactly the stale-at-startup credential this scheme exists to avoid.
        registry = CredentialProviderRegistry()
        provider = _FakeProvider()
        registry.register("fake", lambda params: provider)

        source = credential_source("provider:fake", registry=registry)

        assert source.is_dynamic
        assert provider.calls == 0

    def test_unknown_provider_name_raises(self) -> None:
        registry = CredentialProviderRegistry()
        with pytest.raises(UnknownCredentialProvider):
            credential_source("provider:nope", registry=registry)

    def test_provider_ref_without_a_name_raises(self) -> None:
        with pytest.raises(CredentialError, match="missing a provider name"):
            credential_source("provider:")

    def test_malformed_ref_raises(self) -> None:
        with pytest.raises(CredentialError, match="malformed"):
            credential_source("no-scheme-here")

    def test_unknown_scheme_raises(self) -> None:
        with pytest.raises(CredentialError, match="unknown"):
            credential_source("vault:secret/x")
