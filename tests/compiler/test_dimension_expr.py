"""Compiler tests for derived dimensions (``expr`` on ``dimensions``).

A derived dimension is emitted in SELECT and GROUP BY exactly like a column dimension,
with the parsed expression in place of the bare column. ``granularity`` composes on top of
an ``expr`` of type date/timestamp, which is the case that makes epoch-seconds columns
(e.g. Stripe's ``created``) usable as a time axis.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AllowDenyPolicy,
    CanonicalRef,
    MaskingRule,
    MaskStrategy,
    MetricBinding,
    RoleDef,
    RolePolicy,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource


@pytest.fixture
def charges() -> SemanticSource:
    return SemanticSource(
        name="charges",
        connection="warehouse_pg",
        table="stripe.charges",
        grain=["id"],
        columns=[
            Column(name="id", type="string", nullable=False),
            Column(name="amount", type="int", nullable=False),
            Column(name="created", type="int", nullable=False),
            Column(name="email", type="string", nullable=True),
        ],
        measures=[Measure(name="gross_amount", expr="sum(amount) / 100.0")],
        dimensions=[
            Dimension(
                name="charge_date",
                expr="to_timestamp(created)",
                type="timestamp",
                granularity="day",
            ),
            Dimension(name="email_domain", expr="split_part(email, '@', 2)", type="string"),
        ],
    )


@pytest.fixture
def resolver() -> ContractResolver:
    binding = MetricBinding(
        metric="gross_revenue",
        canonical=CanonicalRef(source="charges", measure="gross_amount"),
    )
    return ContractResolver(bindings=[binding], guardrails=[])


def _select(sql: str) -> exp.Select:
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, exp.Select)
    return parsed


def _projection(select: exp.Select, alias: str) -> exp.Expression:
    for proj in select.expressions:
        if isinstance(proj, exp.Alias) and proj.alias == alias:
            return proj.this
    raise AssertionError(f"no projection aliased {alias!r} in: {select.sql()}")


def test_expr_dimension_in_select_and_group_by(charges, resolver) -> None:
    result = compile(
        SemanticQuery(metrics=["gross_revenue"], dimensions=["email_domain"]), resolver, [charges]
    )
    select = _select(result.sql)
    proj = _projection(select, "email_domain")
    assert "SPLIT_PART" in proj.sql(dialect="postgres").upper()
    assert '"charges"."email"' in proj.sql(dialect="postgres")
    group = select.args["group"]
    assert [g.sql(dialect="postgres") for g in group.expressions] == [proj.sql(dialect="postgres")]


def test_granularity_applies_on_top_of_expr(charges, resolver) -> None:
    result = compile(
        SemanticQuery(metrics=["gross_revenue"], dimensions=["charge_date"]), resolver, [charges]
    )
    proj = _projection(_select(result.sql), "charge_date")
    rendered = proj.sql(dialect="postgres").upper()
    assert "DATE_TRUNC('DAY'" in rendered
    assert "TO_TIMESTAMP" in rendered
    assert '"CHARGES"."CREATED"' in rendered


def test_compile_is_deterministic(charges, resolver) -> None:
    query = SemanticQuery(metrics=["gross_revenue"], dimensions=["charge_date", "email_domain"])
    first = compile(query, resolver, [charges])
    second = compile(query, resolver, [charges])
    assert first.sql == second.sql


def test_expr_dimension_is_recognised_as_time_dimension(charges) -> None:
    by_name = {d.name: d for d in charges.dimensions}
    assert by_name["charge_date"].value_type(charges.columns).value == "timestamp"
    assert by_name["email_domain"].value_type(charges.columns).value == "string"


@pytest.fixture
def masked_resolver() -> ContractResolver:
    binding = MetricBinding(
        metric="gross_revenue",
        canonical=CanonicalRef(source="charges", measure="gross_amount"),
    )
    policy = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="masked_viewer",
        roles={
            "masked_viewer": RoleDef(
                metrics=AllowDenyPolicy(allow=["*"]),
                dimensions=AllowDenyPolicy(allow=["*"]),
                masking=[MaskingRule(column="charges.email", strategy=MaskStrategy.NULL)],
            )
        },
    )
    return ContractResolver(bindings=[binding], guardrails=[], roles=policy)


def test_masking_a_column_masks_dimensions_derived_from_it(charges, masked_resolver) -> None:
    """A derived dimension must not leak a column its role has masked."""
    result = compile(
        SemanticQuery(metrics=["gross_revenue"], dimensions=["email_domain"]),
        masked_resolver,
        [charges],
        principal=Principal(tenant=None, roles=("masked_viewer",)),
    )
    assert isinstance(_projection(_select(result.sql), "email_domain"), exp.Null)


def test_masking_leaves_unrelated_expr_dimension_alone(charges, masked_resolver) -> None:
    result = compile(
        SemanticQuery(metrics=["gross_revenue"], dimensions=["charge_date"]),
        masked_resolver,
        [charges],
        principal=Principal(tenant=None, roles=("masked_viewer",)),
    )
    assert not isinstance(_projection(_select(result.sql), "charge_date"), exp.Null)
