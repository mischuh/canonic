"""Tests for §5.4.2 derive and param-value pattern validation."""

from __future__ import annotations

import pytest

from canonic.exc import PackError
from canonic.packs.derive import apply_derive, validate_param_value
from canonic.packs.manifest import DeriveSpec, ParamValidate


def test_apply_derive_joins_multiple_values():
    spec = DeriveSpec(**{"from": "domains", "per_value": "region != '{{value}}'", "join": " AND "})
    assert apply_derive(spec, "us,eu") == "region != 'us' AND region != 'eu'"


def test_apply_derive_single_value():
    spec = DeriveSpec(**{"from": "domains", "per_value": "region != '{{value}}'"})
    assert apply_derive(spec, "us") == "region != 'us'"


def test_apply_derive_empty_uses_when_empty():
    spec = DeriveSpec(
        **{"from": "domains", "per_value": "region != '{{value}}'", "when_empty": "TRUE"}
    )
    assert apply_derive(spec, "") == "TRUE"
    assert apply_derive(spec, "   ") == "TRUE"


def test_apply_derive_strips_whitespace_around_values():
    spec = DeriveSpec(**{"from": "domains", "per_value": "{{value}}", "join": ","})
    assert apply_derive(spec, " us , eu ") == "us,eu"


def test_validate_param_value_none_pattern_is_noop():
    validate_param_value("x", "anything", None)


def test_validate_param_value_accepts_match():
    validate_param_value("domains", "us,eu", ParamValidate(pattern=r"^[a-z]+(,[a-z]+)*$"))


def test_validate_param_value_rejects_mismatch():
    with pytest.raises(PackError, match="does not match pattern"):
        validate_param_value("domains", "us, eu", ParamValidate(pattern=r"^[a-z]+(,[a-z]+)*$"))
