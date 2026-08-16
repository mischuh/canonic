"""Tests for canonic/contracts/models.py, loader.py, and resolver.py — tenancy/role
policy loading and the resolver seam extensions (SPEC-E12 §1, §2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.contracts.loader import load_role_policy, load_tenancy_policy
from canonic.contracts.models import MaskStrategy, RolePolicy, TenancyPolicy
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver, ScopeRule, Shared, Undeclared
from canonic.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

VALID_TENANCY_YAML = """\
schema: tenancy/v1
claim: merchant_id
on_missing_principal: deny

scoped_sources:
  - { source: orders,      column: merchant_id }
  - { source: order_items, column: merchant_id }
  - { source: customers,   column: merchant_id }

shared_sources:
  - dim_date
  - dim_currency

undeclared_source: deny
"""

VALID_ROLES_YAML = """\
schema: roles/v1
claim: roles
default_role: merchant_viewer

roles:
  merchant_viewer:
    metrics:    { allow: ["revenue", "order_count", "aov"] }
    dimensions: { deny:  ["customer_email", "customer_phone"] }
    knowledge:  { allow_tags: ["public", "merchant"] }
    run_sql:    false
  merchant_admin:
    inherits: merchant_viewer
    dimensions: { deny: [] }
    masking:
      - { column: customers.customer_email, strategy: partial }
  platform_analyst:
    tenancy_exempt: true
    metrics: { allow: ["*"] }
    run_sql: true
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestLoadTenancyPolicy:
    def test_happy_path(self, tmp_path: Path) -> None:
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
        policy = load_tenancy_policy(tmp_path)
        assert policy is not None
        assert policy.claim == "merchant_id"
        assert len(policy.scoped_sources) == 3
        assert policy.shared_sources == ["dim_date", "dim_currency"]

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        assert load_tenancy_policy(tmp_path) is None

    def test_scoped_and_shared_overlap_rejected(self, tmp_path: Path) -> None:
        bad = VALID_TENANCY_YAML.replace("  - dim_date", "  - orders\n  - dim_date")
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", bad)
        with pytest.raises(ContractError, match="orders"):
            load_tenancy_policy(tmp_path)

    def test_bad_schema_value_rejected(self, tmp_path: Path) -> None:
        bad = VALID_TENANCY_YAML.replace("schema: tenancy/v1", "schema: not-a-real-schema")
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", bad)
        with pytest.raises(ContractError):
            load_tenancy_policy(tmp_path)


class TestLoadRolePolicy:
    def test_happy_path(self, tmp_path: Path) -> None:
        _write(tmp_path / "contracts" / "policies" / "roles.yaml", VALID_ROLES_YAML)
        policy = load_role_policy(tmp_path)
        assert policy is not None
        assert policy.default_role == "merchant_viewer"
        assert set(policy.roles) == {"merchant_viewer", "merchant_admin", "platform_analyst"}
        admin = policy.roles["merchant_admin"]
        assert admin.masking[0].strategy is MaskStrategy.PARTIAL

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        assert load_role_policy(tmp_path) is None

    def test_default_role_must_be_declared(self, tmp_path: Path) -> None:
        bad = VALID_ROLES_YAML.replace("default_role: merchant_viewer", "default_role: ghost_role")
        _write(tmp_path / "contracts" / "policies" / "roles.yaml", bad)
        with pytest.raises(ContractError, match="ghost_role"):
            load_role_policy(tmp_path)

    def test_inherits_undeclared_role_rejected(self, tmp_path: Path) -> None:
        bad = VALID_ROLES_YAML.replace("inherits: merchant_viewer", "inherits: ghost_parent")
        _write(tmp_path / "contracts" / "policies" / "roles.yaml", bad)
        with pytest.raises(ContractError, match="ghost_parent"):
            load_role_policy(tmp_path)

    def test_cyclic_inherits_rejected(self, tmp_path: Path) -> None:
        cyclic = """\
schema: roles/v1
claim: roles
roles:
  a: { inherits: b }
  b: { inherits: a }
"""
        _write(tmp_path / "contracts" / "policies" / "roles.yaml", cyclic)
        with pytest.raises(ContractError, match="cyclic"):
            load_role_policy(tmp_path)


class TestTenancyFor:
    def test_no_policy_loaded_is_shared_for_any_source(self) -> None:
        resolver = ContractResolver(bindings=[], guardrails=[])
        assert resolver.tenancy_enabled is False
        result = resolver.tenancy_for("orders")
        assert isinstance(result, Shared)
        assert result.source == "orders"

    def test_scoped_source_returns_scope_rule(self, tmp_path: Path) -> None:
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
        tenancy = load_tenancy_policy(tmp_path)
        resolver = ContractResolver(bindings=[], guardrails=[], tenancy=tenancy)
        assert resolver.tenancy_enabled is True
        result = resolver.tenancy_for("orders")
        assert isinstance(result, ScopeRule)
        assert result.column == "merchant_id"

    def test_shared_source_returns_shared(self, tmp_path: Path) -> None:
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
        tenancy = load_tenancy_policy(tmp_path)
        resolver = ContractResolver(bindings=[], guardrails=[], tenancy=tenancy)
        result = resolver.tenancy_for("dim_date")
        assert isinstance(result, Shared)

    def test_undeclared_source_is_a_distinct_value_not_none(self, tmp_path: Path) -> None:
        """A policy hole must be actionable (Undeclared), never read as 'no restriction'."""
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
        tenancy = load_tenancy_policy(tmp_path)
        resolver = ContractResolver(bindings=[], guardrails=[], tenancy=tenancy)
        result = resolver.tenancy_for("some_untracked_source")
        assert isinstance(result, Undeclared)
        assert result is not None
        assert result.source == "some_untracked_source"


def _resolver_with_roles(tmp_path: Path, roles_yaml: str = VALID_ROLES_YAML) -> ContractResolver:
    _write(tmp_path / "contracts" / "policies" / "roles.yaml", roles_yaml)
    roles = load_role_policy(tmp_path)
    return ContractResolver(bindings=[], guardrails=[], roles=roles)


class TestAuthzFor:
    def test_no_role_policy_loaded_is_unrestricted(self) -> None:
        resolver = ContractResolver(bindings=[], guardrails=[])
        policy = resolver.authz_for(Principal(tenant="4711"))
        assert policy.metric_allowed("anything")
        assert policy.dimension_allowed("anything")
        assert policy.run_sql is True

    def test_no_role_claim_falls_back_to_default_role(self, tmp_path: Path) -> None:
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="4711", roles=()))
        assert policy.roles == ("merchant_viewer",)
        assert policy.metric_allowed("revenue")

    def test_unknown_role_denies_everything(self, tmp_path: Path) -> None:
        """A role claim naming a role absent from the policy fails closed, no default fallback."""
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("ghost_role",)))
        assert policy.roles == ()
        assert not policy.metric_allowed("revenue")
        assert policy.run_sql is False

    def test_explicit_allow_list_only_permits_named_metrics(self, tmp_path: Path) -> None:
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("merchant_viewer",)))
        assert policy.metric_allowed("revenue")
        assert policy.metric_allowed("order_count")
        assert not policy.metric_allowed("cogs")

    def test_deny_wins_over_allow(self, tmp_path: Path) -> None:
        yaml_src = VALID_ROLES_YAML.replace(
            'metrics:    { allow: ["revenue", "order_count", "aov"] }',
            'metrics:    { allow: ["revenue", "order_count", "aov"], deny: ["revenue"] }',
        )
        resolver = _resolver_with_roles(tmp_path, yaml_src)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("merchant_viewer",)))
        assert not policy.metric_allowed("revenue")
        assert policy.metric_allowed("order_count")

    def test_dimensions_default_unrestricted_when_allow_omitted(self, tmp_path: Path) -> None:
        """dimensions declares only `deny` — omitted `allow` must not mean allow-nothing."""
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("merchant_viewer",)))
        assert policy.dimension_allowed("region")
        assert not policy.dimension_allowed("customer_email")
        assert not policy.dimension_allowed("customer_phone")

    def test_explicit_empty_allow_means_allow_nothing(self, tmp_path: Path) -> None:
        yaml_src = VALID_ROLES_YAML.replace(
            'metrics:    { allow: ["revenue", "order_count", "aov"] }',
            "metrics:    { allow: [] }",
        )
        resolver = _resolver_with_roles(tmp_path, yaml_src)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("merchant_viewer",)))
        assert not policy.metric_allowed("revenue")
        assert not policy.metric_allowed("anything")

    def test_wildcard_allow_permits_everything_subject_to_deny(self, tmp_path: Path) -> None:
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="op", roles=("platform_analyst",)))
        assert policy.metric_allowed("revenue")
        assert policy.metric_allowed("literally_anything")
        assert policy.run_sql is True
        assert policy.tenancy_exempt is True

    def test_inheritance_field_override_not_merge(self, tmp_path: Path) -> None:
        """merchant_admin re-opens PII dimensions rather than adding to the parent's deny list."""
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(Principal(tenant="4711", roles=("merchant_admin",)))
        # dimensions replaced wholesale: deny: [] means nothing is denied anymore
        assert policy.dimension_allowed("customer_email")
        assert policy.dimension_allowed("customer_phone")
        # metrics not authored on merchant_admin -> inherited from merchant_viewer untouched
        assert policy.metric_allowed("revenue")
        assert not policy.metric_allowed("cogs")
        # masking carried through
        assert len(policy.masking) == 1
        assert policy.masking[0].column == "customers.customer_email"

    def test_multiple_roles_union_with_deny_winning(self, tmp_path: Path) -> None:
        resolver = _resolver_with_roles(tmp_path)
        policy = resolver.authz_for(
            Principal(tenant="4711", roles=("merchant_viewer", "merchant_admin"))
        )
        assert policy.roles == ("merchant_admin", "merchant_viewer")  # stable-sorted
        # union of run_sql: both false -> false
        assert policy.run_sql is False
        # merchant_viewer's deny of customer_email still applies: deny is a union across
        # roles too, and deny wins, so merchant_admin's open dimensions don't override it
        assert not policy.dimension_allowed("customer_email")
        # a dimension neither role denies is still allowed
        assert policy.dimension_allowed("region")

    def test_stable_ordering_of_masking_rules(self, tmp_path: Path) -> None:
        yaml_src = VALID_ROLES_YAML.replace(
            "    masking:\n      - { column: customers.customer_email, strategy: partial }\n",
            "    masking:\n"
            "      - { column: customers.customer_phone, strategy: hash }\n"
            "      - { column: customers.customer_email, strategy: partial }\n",
        )
        resolver = _resolver_with_roles(tmp_path, yaml_src)
        policy1 = resolver.authz_for(Principal(tenant="t", roles=("merchant_admin",)))
        policy2 = resolver.authz_for(Principal(tenant="t", roles=("merchant_admin",)))
        assert policy1.masking == policy2.masking
        assert [m.column for m in policy1.masking] == sorted(m.column for m in policy1.masking)


class TestFromProjectLoadsPolicies:
    def test_from_project_loads_both_policies(self, tmp_path: Path) -> None:
        _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
        _write(tmp_path / "contracts" / "policies" / "roles.yaml", VALID_ROLES_YAML)
        resolver = ContractResolver.from_project(tmp_path)
        assert resolver.tenancy_enabled is True
        assert isinstance(resolver.tenancy_for("orders"), ScopeRule)
        policy = resolver.authz_for(Principal(tenant="4711"))
        assert policy.roles == ("merchant_viewer",)

    def test_from_project_without_policies_is_unrestricted(self, tmp_path: Path) -> None:
        resolver = ContractResolver.from_project(tmp_path)
        assert resolver.tenancy_enabled is False
        assert isinstance(resolver.tenancy_for("orders"), Shared)


def test_tenancy_policy_and_role_policy_types_round_trip(tmp_path: Path) -> None:
    _write(tmp_path / "contracts" / "policies" / "tenancy.yaml", VALID_TENANCY_YAML)
    _write(tmp_path / "contracts" / "policies" / "roles.yaml", VALID_ROLES_YAML)
    tenancy = load_tenancy_policy(tmp_path)
    roles = load_role_policy(tmp_path)
    assert isinstance(tenancy, TenancyPolicy)
    assert isinstance(roles, RolePolicy)
