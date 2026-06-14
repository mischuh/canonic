"""Tests for the canonical error registry (SPEC E7+E8 §6.1)."""

from __future__ import annotations

import pytest

from canon import exc
from canon.exc import EXIT_CODES, CanonError, ErrorCode

# (exception class, expected exit code) for every registry entry.
_REGISTRY: list[tuple[type[CanonError], int]] = [
    (exc.Unresolved, 2),
    (exc.Ambiguous, 3),
    (exc.Unreachable, 4),
    (exc.AmbiguousJoinPath, 5),
    (exc.UnsupportedMeasure, 6),
    (exc.FanoutUnsafe, 7),
    (exc.GuardrailBlock, 8),
    (exc.ValidationFailed, 9),
    (exc.AssertionFailed, 10),
    (exc.ReadOnlyViolation, 11),
    (exc.SchemaMismatch, 12),
    (exc.ConnectionError, 13),
]


def test_every_error_code_has_a_unique_exit_value() -> None:
    assert sorted(EXIT_CODES.values()) == list(range(2, 14))
    assert set(EXIT_CODES) == set(ErrorCode)


@pytest.mark.parametrize(("error_cls", "expected_exit"), _REGISTRY)
def test_subclass_exit_code(error_cls: type[CanonError], expected_exit: int) -> None:
    assert error_cls("boom").exit_code == expected_exit


def test_base_error_without_code_uses_exit_one() -> None:
    assert CanonError("boom").exit_code == 1


def test_errors_without_registry_code_use_exit_one() -> None:
    # CredentialError / SemanticSourceError carry no §6.1 code.
    assert exc.CredentialError("x").exit_code == 1
    assert exc.SemanticSourceError("x").exit_code == 1


def test_all_subclasses_derive_from_canon_error() -> None:
    for error_cls, _ in _REGISTRY:
        assert issubclass(error_cls, CanonError)
