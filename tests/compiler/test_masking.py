"""Compiler tests for role-based column masking (SPEC-E12 §1.2, Phase 7).

`roles.yaml`'s `masking` rules parse inert from Phase 1 onward (AMENDMENT
"P2 within P2") — this is the enforcement half: a role's masking rules rewrite the
SELECT/GROUP-BY expression for the matching dimension at compile time, per
`rule.strategy`. Every strategy path is exercised through `_dimension_expr`'s call
sites: the plain single-SELECT builder, the fanout dedup subquery, recompute_at_grain,
and semi_additive — the last of which also proves masking never reaches the internal
grain columns a window function partitions by (that would silently corrupt which row
"last per entity" picks).
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AllowDenyPolicy,
    BindingKind,
    CanonicalRef,
    CollapseAgg,
    MaskingRule,
    MaskStrategy,
    MetricBinding,
    RoleDef,
    RolePolicy,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource


def _select(sql: str) -> exp.Select:
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, exp.Select)
    return parsed


def _projection(select: exp.Select, alias: str) -> exp.Expression:
    for proj in select.expressions:
        if isinstance(proj, exp.Alias) and proj.alias == alias:
            return proj.this
    raise AssertionError(f"no projection aliased {alias!r} in: {select.sql()}")


@pytest.fixture
def role_policy_factory():
    def make(masking: list[MaskingRule], *, tenancy_exempt: bool = False) -> RolePolicy:
        return RolePolicy(
            schema_="roles/v1",
            claim="roles",
            default_role="masked_viewer",
            roles={
                "masked_viewer": RoleDef(
                    metrics=AllowDenyPolicy(allow=["*"]),
                    dimensions=AllowDenyPolicy(allow=["*"]),
                    tenancy_exempt=tenancy_exempt,
                    masking=masking,
                ),
            },
        )

    return make


@pytest.fixture
def resolver_factory(revenue_binding: MetricBinding, role_policy_factory):
    def make(masking: list[MaskingRule], *, tenancy_exempt: bool = False) -> ContractResolver:
        return ContractResolver(
            bindings=[revenue_binding],
            guardrails=[],
            roles=role_policy_factory(masking, tenancy_exempt=tenancy_exempt),
        )

    return make


@pytest.fixture
def masked_principal() -> Principal:
    return Principal(tenant=None, roles=("masked_viewer",))


def test_null_strategy_replaces_dimension_with_null(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.NULL)]
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    select = _select(result.sql)
    assert isinstance(_projection(select, "region"), exp.Null)
    group = select.args.get("group")
    assert group is not None
    assert not any(isinstance(g, exp.Column) for g in group.expressions)


def test_hash_strategy_wraps_in_md5(resolver_factory, sources, masked_principal: Principal) -> None:
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.HASH)]
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    proj = _projection(_select(result.sql), "region")
    assert isinstance(proj, exp.MD5)
    assert '"customers"."region"' in proj.sql(dialect="postgres")


def test_partial_strategy_wraps_in_substring_concat(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.PARTIAL)]
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    proj = _projection(_select(result.sql), "region")
    # Re-parsing the emitted ``||`` SQL text yields sqlglot's DPipe node, not the Concat
    # node the compiler built it from — the same AST, round-tripped through text.
    assert isinstance(proj, exp.DPipe)
    rendered = proj.sql(dialect="postgres")
    assert "SUBSTRING" in rendered
    assert "***" in rendered


def test_dimension_not_named_by_a_rule_is_untouched(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    """Masking one column on a source never leaks onto its sibling columns."""
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.NULL)]
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region", "status"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    proj = _projection(_select(result.sql), "status")
    assert isinstance(proj, exp.Column)


def test_no_role_policy_is_unaffected(resolver, sources) -> None:
    """Absence is the feature switch: a project with no roles.yaml compiles unchanged."""
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
    )
    proj = _projection(_select(result.sql), "region")
    assert isinstance(proj, exp.Column)


def test_role_with_no_masking_rules_is_unaffected(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    resolver = resolver_factory([])
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    proj = _projection(_select(result.sql), "region")
    assert isinstance(proj, exp.Column)


def test_fanout_masks_once_in_the_inner_dedup_subquery(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    """The outer aggregate over a fanning join only ever references the inner alias, so
    masking must land in the inner ``DISTINCT ON`` projection — applying it twice (or in
    the outer SELECT instead) would either double-wrap or silently do nothing.
    """
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.HASH)]
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["sku", "region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    assert result.sql.count("MD5(") == 1
    inner = result.sql.split("FROM (")[1]
    assert "MD5(" in inner.split(') AS "_base"')[0]


def test_tenancy_exempt_role_still_masks(
    resolver_factory, sources, masked_principal: Principal
) -> None:
    """tenancy_exempt bypasses row-level tenant scoping only — RBAC masking is orthogonal
    and still applies to the platform operator's own role, per SPEC-E12 §1.2.
    """
    resolver = resolver_factory(
        [MaskingRule(column="customers.region", strategy=MaskStrategy.NULL)], tenancy_exempt=True
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        sources,
        principal=masked_principal,
    )
    assert isinstance(_projection(_select(result.sql), "region"), exp.Null)


def test_recompute_at_grain_masks_the_dimension(sources) -> None:
    binding = MetricBinding(
        metric="distinct_customers",
        canonical=CanonicalRef(
            kind=BindingKind.DISTINCT_COUNT, source="orders", distinct_on="customer_id"
        ),
    )
    role_policy = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="masked_viewer",
        roles={
            "masked_viewer": RoleDef(
                metrics=AllowDenyPolicy(allow=["*"]),
                dimensions=AllowDenyPolicy(allow=["*"]),
                masking=[MaskingRule(column="customers.region", strategy=MaskStrategy.PARTIAL)],
            ),
        },
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[], roles=role_policy)
    result = compile(
        SemanticQuery(metrics=["distinct_customers"], dimensions=["region"]),
        resolver,
        sources,
        principal=Principal(tenant=None, roles=("masked_viewer",)),
    )
    proj = _projection(_select(result.sql), "region")
    # Re-parsing the emitted ``||`` SQL text yields sqlglot's DPipe node, not the Concat
    # node the compiler built it from — the same AST, round-tripped through text.
    assert isinstance(proj, exp.DPipe)


def test_semi_additive_masks_projection_but_not_the_partition(sources) -> None:
    """A collapse dimension masked by a role appears masked in SELECT/GROUP BY, but the
    ROW_NUMBER window's PARTITION BY must keep operating on the true entity key — masking
    it would corrupt which row "last per entity" resolves to (SPEC §4.2).
    """
    inventory = SemanticSource(
        name="inventory_snapshots",
        connection="warehouse_pg",
        table="analytics.inventory_snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="inventory_level", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="inventory_level", expr="sum(inventory_level)", additivity="additive")
        ],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )
    binding = MetricBinding(
        metric="ending_inventory",
        canonical=CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="inventory_snapshots",
            measure="inventory_level",
            collapse_dimension="snapshot_date",
            collapse_agg=CollapseAgg.LAST,
        ),
    )
    role_policy = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="masked_viewer",
        roles={
            "masked_viewer": RoleDef(
                metrics=AllowDenyPolicy(allow=["*"]),
                dimensions=AllowDenyPolicy(allow=["*"]),
                masking=[
                    MaskingRule(
                        column="inventory_snapshots.warehouse_id", strategy=MaskStrategy.HASH
                    )
                ],
            ),
        },
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[], roles=role_policy)
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        resolver,
        [inventory],
        principal=Principal(tenant=None, roles=("masked_viewer",)),
    )
    sqlglot.parse_one(result.sql, dialect="postgres")  # valid SQL
    inner_cte = result.sql.split("SELECT ")[1].split(" FROM")[0]
    assert "MD5(" in inner_cte
    partition_clause = result.sql.split("PARTITION BY")[1].split("ORDER BY")[0]
    assert "MD5(" not in partition_clause
    assert '"inventory_snapshots"."warehouse_id"' in partition_clause
