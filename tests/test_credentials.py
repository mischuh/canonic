"""Tests for canonic/credentials.py — credentials_ref resolution (E1 §3/§7, #65)."""

from __future__ import annotations

import pytest

from canonic.credentials import resolve_credential
from canonic.exc import CredentialError


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
