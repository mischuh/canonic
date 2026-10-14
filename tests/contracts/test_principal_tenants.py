"""``normalize_tenants`` and ``Principal.tenant_scope``: how a tenant claim becomes a principal."""

from __future__ import annotations

import pytest

from canonic.contracts.principal import Principal, normalize_tenants
from canonic.exc import TenantLimitExceeded, TenantUnresolved


def test_scalar_becomes_one_element_tuple() -> None:
    assert normalize_tenants("4711") == ("4711",)
    assert normalize_tenants(4711) == (4711,)


def test_list_is_deduplicated_and_ordered_numbers_before_strings() -> None:
    assert normalize_tenants(["b", 2, "a", 1, "b", 2]) == (1, 2, "a", "b")


def test_integral_float_becomes_integer() -> None:
    assert normalize_tenants([4711.0]) == (4711,)


@pytest.mark.parametrize("raw", [None, [], [None], ["", "  ", None], ""])
def test_blank_input_resolves_to_no_tenant(raw: object) -> None:
    assert normalize_tenants(raw) == ()


@pytest.mark.parametrize("bad", [True, 1.5, {"a": 1}, ["x"], object()])
def test_value_that_cannot_be_a_tenant_raises(bad: object) -> None:
    with pytest.raises(TenantUnresolved):
        normalize_tenants([bad])


def test_limit_counts_distinct_values() -> None:
    assert normalize_tenants(["a", "a", "b"], limit=2) == ("a", "b")
    with pytest.raises(TenantLimitExceeded):
        normalize_tenants(["a", "b", "c"], limit=2)


@pytest.mark.parametrize(
    ("tenants", "expected"),
    [((), None), (("4711",), "4711"), ((4711,), 4711), (("a", "b"), ["a", "b"])],
)
def test_tenant_scope_reports_one_value_bare_and_several_as_a_list(
    tenants: tuple[str | int, ...], expected: object
) -> None:
    assert Principal(tenants=tenants).tenant_scope == expected
