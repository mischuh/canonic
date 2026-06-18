"""Tests for canon/airgap.py — the air-gapped egress guard (SPEC-E10 §4, GH-63)."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

from canon.airgap import LOCAL_REF_SCHEMES, EgressPolicy
from canon.exc import AirGappedViolation, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Sequence


# --- is_allowed_host: literals & localhost ------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.6.7", "::1", "localhost"])
def test_loopback_and_localhost_always_allowed(host: str) -> None:
    assert EgressPolicy().is_allowed_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "93.184.216.34", "10.1.2.3", "192.168.1.5"])
def test_public_and_unlisted_private_literals_rejected_by_default(host: str) -> None:
    # Default policy is localhost-only: even a private LAN address is rejected until
    # it is explicitly allowlisted.
    assert EgressPolicy().is_allowed_host(host) is False


def test_allow_cidrs_opts_in_a_lan_range() -> None:
    policy = EgressPolicy(allow_cidrs=["10.0.0.0/8"])
    assert policy.is_allowed_host("10.1.2.3") is True
    # A private host outside the listed range is still rejected.
    assert policy.is_allowed_host("192.168.1.5") is False


def test_malformed_cidr_is_rejected_at_construction() -> None:
    with pytest.raises(AirGappedViolation, match="not a valid CIDR"):
        EgressPolicy(allow_cidrs=["not-a-cidr"])


# --- is_allowed_host: hostname resolution (fail-closed) -----------------------


def _patch_getaddrinfo(monkeypatch: pytest.MonkeyPatch, addresses: Sequence[str]) -> None:
    def fake(host: str, *args: object, **kwargs: object) -> list[tuple]:
        return [(None, None, None, "", (addr, 0)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_hostname_resolving_to_loopback_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_getaddrinfo(monkeypatch, ["127.0.0.1"])
    assert EgressPolicy().is_allowed_host("my-local-llm") is True


def test_hostname_resolving_to_public_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_getaddrinfo(monkeypatch, ["93.184.216.34"])
    assert EgressPolicy().is_allowed_host("evil.example.com") is False


def test_hostname_with_any_public_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every resolved address must be allowlisted — one public address fails the whole host.
    _patch_getaddrinfo(monkeypatch, ["127.0.0.1", "93.184.216.34"])
    assert EgressPolicy().is_allowed_host("split-horizon") is False


def test_unresolvable_hostname_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> list[tuple]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert EgressPolicy().is_allowed_host("nope.invalid") is False


# --- check_url ----------------------------------------------------------------


def test_check_url_allows_local_endpoint() -> None:
    EgressPolicy().check_url("http://localhost:11434/v1", what="llm.base_url")  # no raise


def test_check_url_blocks_public_endpoint() -> None:
    with pytest.raises(AirGappedViolation) as exc:
        EgressPolicy().check_url("https://api.openai.com/v1", what="llm.base_url")
    assert exc.value.code is ErrorCode.AIR_GAPPED_VIOLATION
    assert exc.value.exit_code == 18
    assert "llm.base_url" in str(exc.value)


def test_check_url_without_host_is_rejected() -> None:
    with pytest.raises(AirGappedViolation, match="no resolvable host"):
        EgressPolicy().check_url("not-a-url", what="llm.base_url")


# --- check_ref_local ----------------------------------------------------------


@pytest.mark.parametrize("ref", ["env:CANON_LLM_KEY", "file:.canon/secret", "keyring:canon"])
def test_check_ref_local_allows_local_schemes(ref: str) -> None:
    EgressPolicy().check_ref_local(ref, what="llm.api_key_ref")  # no raise


@pytest.mark.parametrize("ref", ["vault:secret/llm", "https://secrets.example/llm"])
def test_check_ref_local_rejects_remote_schemes(ref: str) -> None:
    with pytest.raises(AirGappedViolation, match="non-local secret scheme"):
        EgressPolicy().check_ref_local(ref, what="llm.api_key_ref")


def test_local_ref_schemes_match_credentials_layer() -> None:
    # Guard against drift: the local schemes mirror what credentials.py recognizes.
    assert set(LOCAL_REF_SCHEMES) == {"env", "keyring", "file"}
