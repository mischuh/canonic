"""Compiler tests for tenant scoping / RBAC compiler integration (SPEC-E12 §3, Phase 3).

Acceptance criteria covered here (AMENDMENT-tenant-scoping-rbac.md §8):
  S11 — rows are scoped to the principal's tenant (AC1, AC2).
  S12 — every scoped source in the join path is filtered (AC1, AC2).
  S13 — the system fails closed (AC1, AC2; AC3 is stdio-refusal, covered in test_daemon.py).
  S14 — the tenant cannot be supplied by the caller (AC1, AC2, AC3).
  S16 — isolation is provable after the fact, via metadata.scope (AC3 here; AC1/AC2 are
        AnswerEvent fields, covered in tests/instrumentation/test_answer_event.py).

Also covers RBAC metric filtering (compiler stage 1) and the tenancy_exempt / allow_unscoped
escape hatches, which S11-S16 assume but do not separately number.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AllowDenyPolicy,
    CanonicalRef,
    MetricBinding,
    OnMissingPrincipal,
    RoleDef,
    RolePolicy,
    ScopedSource,
    TenancyPolicy,
    UndeclaredSource,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.exc import TenantScopeMissing, TenantUnresolved, Unresolved
from canonic.semantic.models import (
    Column,
    Dimension,
    Join,
    Measure,
    NormalizedType,
    Relationship,
    SemanticSource,
)

# Shared fixtures — orders (scoped) -> customers (scoped), dim_date (shared),
# promotions (undeclared — in neither list, a deliberate policy hole).


@pytest.fixture
def orders() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="merchant_id", type="string", nullable=False),
            Column(name="promo_id", type="string", nullable=True),
        ],
        measures=[Measure(name="revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[],
        joins=[
            Join(
                to="customers",
                on="orders.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="dim_date",
                on="orders.order_id = dim_date.date_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="promotions",
                on="orders.promo_id = promotions.promo_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )


@pytest.fixture
def customers() -> SemanticSource:
    return SemanticSource(
        name="customers",
        connection="warehouse_pg",
        table="dim_customers",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="name", type="string", nullable=False),
            Column(name="merchant_id", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="name", column="name")],
    )


@pytest.fixture
def dim_date() -> SemanticSource:
    return SemanticSource(
        name="dim_date",
        connection="warehouse_pg",
        table="dim_date",
        grain=["date_id"],
        columns=[
            Column(name="date_id", type="string", nullable=False),
            Column(name="day_name", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="day_name", column="day_name")],
    )


@pytest.fixture
def promotions() -> SemanticSource:
    """Declared in neither scoped_sources nor shared_sources — a deliberate policy hole."""
    return SemanticSource(
        name="promotions",
        connection="warehouse_pg",
        table="dim_promotions",
        grain=["promo_id"],
        columns=[
            Column(name="promo_id", type="string", nullable=False),
            Column(name="promo_code", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="promo_code", column="promo_code")],
    )


@pytest.fixture
def sources(orders, customers, dim_date, promotions) -> list[SemanticSource]:
    return [orders, customers, dim_date, promotions]


@pytest.fixture
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )


@pytest.fixture
def tenancy_policy_deny() -> TenancyPolicy:
    return TenancyPolicy(
        schema_="tenancy/v1",
        claim="merchant_id",
        on_missing_principal=OnMissingPrincipal.DENY,
        scoped_sources=[
            ScopedSource(source="orders", column="merchant_id"),
            ScopedSource(source="customers", column="merchant_id"),
        ],
        shared_sources=["dim_date"],
        undeclared_source=UndeclaredSource.DENY,
    )


@pytest.fixture
def role_policy() -> RolePolicy:
    return RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="merchant_viewer",
        roles={
            "merchant_viewer": RoleDef(metrics=AllowDenyPolicy(allow=["revenue"])),
            "platform_analyst": RoleDef(tenancy_exempt=True, metrics=AllowDenyPolicy(allow=["*"])),
        },
    )


@pytest.fixture
def resolver(revenue_binding, tenancy_policy_deny, role_policy) -> ContractResolver:
    return ContractResolver(
        bindings=[revenue_binding],
        guardrails=[],
        tenancy=tenancy_policy_deny,
        roles=role_policy,
    )


def _where_clause(sql: str) -> exp.Expression:
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    where = parsed.find(exp.Where)
    assert where is not None
    return where.this


def _tenant_predicate(alias: str, column: str, tenant: str) -> str:
    """Render the quoted-identifier form the postgres adapter emits for a tenant predicate."""
    return f'"{alias}"."{column}" = \'{tenant}\''


def test_s11_ac1_tenant_predicate_in_sql(resolver: ContractResolver, sources) -> None:
    principal = Principal(tenants=("4711",), roles=("merchant_viewer",))
    result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources, principal=principal)
    sqlglot.parse_one(result.sql, dialect="postgres")  # valid SQL
    assert _tenant_predicate("orders", "merchant_id", "4711") in result.sql
    assert result.scope is not None
    assert result.scope.tenant == "4711"
    assert result.scope.scoped_sources == ["orders"]


def test_s11_ac2_byte_identical_except_tenant_literal(resolver: ContractResolver, sources) -> None:
    query = SemanticQuery(metrics=["revenue"])
    sql_4711 = compile(query, resolver, sources, principal=Principal(tenants=("4711",))).sql
    sql_4712 = compile(query, resolver, sources, principal=Principal(tenants=("4712",))).sql
    assert sql_4711 != sql_4712
    assert sql_4711.replace("4711", "4712") == sql_4712


def test_s12_ac1_both_scoped_sources_filtered(resolver: ContractResolver, sources) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["name"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    assert _tenant_predicate("orders", "merchant_id", "4711") in result.sql
    assert _tenant_predicate("customers", "merchant_id", "4711") in result.sql
    assert result.scope is not None
    assert result.scope.scoped_sources == ["customers", "orders"]


def test_s12_ac2_shared_source_gets_no_predicate(resolver: ContractResolver, sources) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["day_name"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    assert "dim_date.merchant_id" not in result.sql
    assert result.scope is not None
    assert "dim_date" in result.scope.shared_sources
    assert "dim_date" not in result.scope.scoped_sources


def test_s13_ac1_no_principal_raises_tenant_unresolved(resolver: ContractResolver, sources) -> None:
    with pytest.raises(TenantUnresolved):
        compile(SemanticQuery(metrics=["revenue"]), resolver, sources)


def test_s13_ac2_undeclared_source_raises_tenant_scope_missing(
    resolver: ContractResolver, sources
) -> None:
    with pytest.raises(TenantScopeMissing):
        compile(
            SemanticQuery(metrics=["revenue"], dimensions=["promo_code"]),
            resolver,
            sources,
            principal=Principal(tenants=("4711",)),
        )


def test_undeclared_source_warns_instead_of_raising(
    revenue_binding: MetricBinding, role_policy: RolePolicy, sources
) -> None:
    warn_policy = TenancyPolicy(
        schema_="tenancy/v1",
        claim="merchant_id",
        on_missing_principal=OnMissingPrincipal.DENY,
        scoped_sources=[ScopedSource(source="orders", column="merchant_id")],
        shared_sources=["dim_date", "customers"],
        undeclared_source=UndeclaredSource.WARN,
    )
    resolver = ContractResolver(
        bindings=[revenue_binding], guardrails=[], tenancy=warn_policy, roles=role_policy
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["promo_code"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    assert any("promotions" in w for w in result.warnings)


def test_allow_unscoped_serves_without_tenant_and_warns(
    revenue_binding: MetricBinding, role_policy: RolePolicy, sources
) -> None:
    dev_policy = TenancyPolicy(
        schema_="tenancy/v1",
        claim="merchant_id",
        on_missing_principal=OnMissingPrincipal.ALLOW_UNSCOPED,
        scoped_sources=[ScopedSource(source="orders", column="merchant_id")],
        shared_sources=["dim_date", "customers"],
        undeclared_source=UndeclaredSource.WARN,
    )
    resolver = ContractResolver(
        bindings=[revenue_binding], guardrails=[], tenancy=dev_policy, roles=role_policy
    )
    result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
    assert "merchant_id" not in result.sql.lower()
    assert any("allow_unscoped" in w for w in result.warnings)
    assert result.scope is not None
    assert result.scope.scoped_sources == []


def test_s14_ac1_caller_filter_cannot_widen_scope(resolver: ContractResolver, sources) -> None:
    """A contradicting caller filter is AND-ed alongside the tenant predicate, not instead of it."""
    result = compile(
        SemanticQuery(metrics=["revenue"], filters=["merchant_id = '9999'"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    assert _tenant_predicate("orders", "merchant_id", "4711") in result.sql
    assert "'9999'" in result.sql
    where = _where_clause(result.sql)
    assert isinstance(where, exp.And)


def test_s14_ac3_crafted_filter_cannot_escape_tenant_predicate(
    resolver: ContractResolver, sources
) -> None:
    """A classic injection payload in a filter string cannot detach the tenant predicate,
    since it is a separate AST node ANDed at the top level, never string-interpolated."""
    result = compile(
        SemanticQuery(metrics=["revenue"], filters=["customer_id = '1' OR 1 = 1"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    where = _where_clause(result.sql)
    assert isinstance(where, exp.And)
    rendered = where.sql(dialect="postgres")
    # The tenant predicate must appear as its own top-level conjunct, not swallowed into
    # the OR the crafted filter tried to build.
    assert _tenant_predicate("orders", "merchant_id", "4711") in rendered
    tenant_eq = next(
        n
        for n in where.find_all(exp.EQ)
        if isinstance(n.this, exp.Column) and n.this.name == "merchant_id"
    )
    # Walk up from the tenant EQ node: every ancestor up to the WHERE's own And must be
    # And, never Or — i.e. it is never inside the crafted OR clause.
    node = tenant_eq
    while node is not where:
        parent = node.parent
        assert not isinstance(parent, exp.Or)
        node = parent


def test_tenancy_exempt_role_bypasses_scoping_entirely(resolver: ContractResolver, sources) -> None:
    principal = Principal(roles=("platform_analyst",))
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["name"]),
        resolver,
        sources,
        principal=principal,
    )
    assert "merchant_id" not in result.sql.lower()
    assert result.scope is not None
    assert result.scope.tenancy_exempt is True
    assert result.scope.scoped_sources == []


def test_denied_metric_raises_unresolved_like_a_missing_one(
    resolver: ContractResolver, sources
) -> None:
    principal = Principal(tenants=("4711",), roles=("merchant_viewer",))
    with pytest.raises(Unresolved):
        compile(
            SemanticQuery(metrics=["nonexistent_metric"]), resolver, sources, principal=principal
        )

    # merchant_viewer's allow list is exactly ["revenue"]; a real-but-denied metric name
    # must fail with the identical exception type, never a distinct "forbidden" code.
    denied_role = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="restricted",
        roles={"restricted": RoleDef(metrics=AllowDenyPolicy(allow=[]))},
    )
    restricted_resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
            )
        ],
        guardrails=[],
        roles=denied_role,
    )
    with pytest.raises(Unresolved):
        compile(SemanticQuery(metrics=["revenue"]), restricted_resolver, sources)


def test_no_policy_loaded_is_a_no_op(revenue_binding: MetricBinding, sources) -> None:
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[])
    result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
    assert "merchant_id" not in result.sql.lower()
    assert result.scope is not None
    assert result.scope.tenant is None
    assert result.scope.scoped_sources == []


def _scope_column_as(source: SemanticSource, column_type: NormalizedType) -> SemanticSource:
    """``source`` with its ``merchant_id`` column retyped, everything else untouched."""
    columns = [
        c.model_copy(update={"type": column_type}) if c.name == "merchant_id" else c
        for c in source.columns
    ]
    return source.model_copy(update={"columns": columns})


def test_tenant_list_compiles_to_in_on_every_scoped_source(
    resolver: ContractResolver, sources
) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["name", "day_name"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711", "4712")),
    )
    sqlglot.parse_one(result.sql, dialect="postgres")
    assert "\"orders\".\"merchant_id\" IN ('4711', '4712')" in result.sql
    assert "\"customers\".\"merchant_id\" IN ('4711', '4712')" in result.sql
    assert "dim_date.merchant_id" not in result.sql
    assert result.scope is not None
    assert result.scope.tenant == ["4711", "4712"]
    assert result.scope.scoped_sources == ["customers", "orders"]


def test_tenant_list_order_and_duplicates_do_not_change_the_sql(
    resolver: ContractResolver, sources
) -> None:
    query = SemanticQuery(metrics=["revenue"])
    canonical = compile(query, resolver, sources, principal=Principal(tenants=("4711", "4712")))
    shuffled = compile(
        query, resolver, sources, principal=Principal(tenants=("4712", "4711", "4712"))
    )
    assert shuffled.sql == canonical.sql


def test_single_element_list_is_the_scalar_predicate(resolver: ContractResolver, sources) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711",)),
    )
    assert _tenant_predicate("orders", "merchant_id", "4711") in result.sql
    assert " IN (" not in result.sql
    assert result.scope is not None
    assert result.scope.tenant == "4711"


def test_crafted_tenant_element_is_bound_as_a_value(resolver: ContractResolver, sources) -> None:
    """A tenant value shaped like an injection stays one string literal inside the IN list."""
    crafted = "x') OR (1=1"
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711", crafted)),
    )
    where = _where_clause(result.sql)
    assert not list(where.find_all(exp.Or))
    in_nodes = list(where.find_all(exp.In))
    assert len(in_nodes) == 1
    literals = [lit.this for lit in in_nodes[0].expressions]
    assert sorted(literals) == sorted(["4711", crafted])


def test_tenant_values_follow_the_scope_column_type(
    resolver: ContractResolver, orders, customers, dim_date, promotions
) -> None:
    """Strings from the IdP match an integer column, and the reverse, per scoped source."""
    int_orders = _scope_column_as(orders, NormalizedType.INT)
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["name"]),
        resolver,
        [int_orders, customers, dim_date, promotions],
        principal=Principal(tenants=("4712", 4711)),
    )
    assert '"orders"."merchant_id" IN (4711, 4712)' in result.sql
    assert "\"customers\".\"merchant_id\" IN ('4711', '4712')" in result.sql


def test_same_tenant_as_string_and_integer_collapses_to_one_value(
    resolver: ContractResolver, orders, customers, dim_date, promotions
) -> None:
    int_orders = _scope_column_as(orders, NormalizedType.INT)
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        [int_orders, customers, dim_date, promotions],
        principal=Principal(tenants=("4711", 4711)),
    )
    assert '"orders"."merchant_id" = 4711' in result.sql
    assert " IN (" not in result.sql


def test_non_numeric_tenant_on_integer_column_fails_closed(
    resolver: ContractResolver, orders, customers, dim_date, promotions
) -> None:
    int_orders = _scope_column_as(orders, NormalizedType.INT)
    with pytest.raises(TenantUnresolved):
        compile(
            SemanticQuery(metrics=["revenue"]),
            resolver,
            [int_orders, customers, dim_date, promotions],
            principal=Principal(tenants=("4711", "abc")),
        )


def test_empty_tenant_list_is_tenant_unresolved(resolver: ContractResolver, sources) -> None:
    with pytest.raises(TenantUnresolved):
        compile(
            SemanticQuery(metrics=["revenue"]),
            resolver,
            sources,
            principal=Principal(tenants=(), roles=("merchant_viewer",)),
        )


def test_tenancy_exempt_role_skips_the_tenant_list(resolver: ContractResolver, sources) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        principal=Principal(tenants=("4711", "4712"), roles=("platform_analyst",)),
    )
    assert "merchant_id" not in result.sql.lower()
    assert result.scope is not None
    assert result.scope.tenancy_exempt is True
    assert result.scope.scoped_sources == []
