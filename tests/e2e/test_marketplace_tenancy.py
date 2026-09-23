"""End-to-end proof that ``examples/marketplace`` actually enforces SPEC-E12 tenant
scoping and role-based authorization (GH docs/marketplace deliverable).

No live infrastructure needed: the project's SQLite database is built once per
session from the tracked ``setup.sql``, exactly like ``tests/golden/conftest.py``
does for ``rental``. Rather than a hand-copied fixture, this loads
``examples/marketplace`` directly — the example a user actually runs never drifts
from what these tests exercise.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.compiler.query import SemanticQuery
from canonic.contracts.principal import Principal
from canonic.core.service import CanonicService
from canonic.exc import (
    TenantForbidden,
    TenantScopeMissing,
    TenantUnresolved,
    Unreachable,
    Unresolved,
)

_EXAMPLE = Path(__file__).parents[2] / "examples" / "marketplace"

_MERCHANT_A = "byte-gadgets"
_MERCHANT_B = "urban-threads"

_VIEWER = Principal(tenant=_MERCHANT_A, roles=("merchant_viewer",))
_VIEWER_B = Principal(tenant=_MERCHANT_B, roles=("merchant_viewer",))
_ADMIN = Principal(tenant=_MERCHANT_A, roles=("merchant_admin",))
_PLATFORM = Principal(tenant=None, roles=("platform_analyst",))
_NO_PRINCIPAL = None


def _build_project(dest: Path) -> Path:
    shutil.copytree(
        _EXAMPLE,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".canonic", "*.wal", ".DS_Store", "marketplace.db"),
    )
    db_path = dest / "marketplace.db"
    setup_sql = (_EXAMPLE / "setup.sql").read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(setup_sql)
        conn.commit()
    finally:
        conn.close()
    return dest


@pytest.fixture(scope="session")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A session-scoped copy of examples/marketplace with a freshly built database."""
    return _build_project(tmp_path_factory.mktemp("marketplace"))


@pytest.fixture(scope="session")
def service(project: Path) -> CanonicService:
    return CanonicService.from_project(project)


@pytest.fixture
def rls_enforced_project(tmp_path: Path) -> Path:
    """A private copy of the project with ``rls_enforced: true`` on the connection.

    Isolated per-test (not session-scoped) since it mutates config on top of the
    shared build, mirroring ``tests/e2e/conftest.py``'s ``e2e_project`` mechanics
    minus the container-coordinate rewrite (SQLite needs none).
    """
    root = _build_project(tmp_path / "project")
    config_path = root / "canonic.yaml"
    yaml = YAML()
    data = yaml.load(config_path.read_text())
    data["connections"][0]["rls_enforced"] = True
    with config_path.open("w") as f:
        yaml.dump(data, f)
    return root


@pytest.fixture
def rls_enforced_service(rls_enforced_project: Path) -> CanonicService:
    return CanonicService.from_project(rls_enforced_project)


# ----------------------------------------------------------------------------
# Merchant isolation
# ----------------------------------------------------------------------------


async def test_merchant_isolation(service: CanonicService) -> None:
    """Two merchants querying the same metric see disjoint, non-empty results."""
    query = SemanticQuery(metrics=["revenue"])

    result_a = await service.query(query, principal=_VIEWER)
    result_b = await service.query(query, principal=_VIEWER_B)

    assert result_a.result.rows, "expected at least one row for merchant A"
    assert result_b.result.rows, "expected at least one row for merchant B"
    assert result_a.result.rows != result_b.result.rows
    assert result_a.metadata.scope is not None
    assert result_b.metadata.scope is not None
    assert result_a.metadata.scope.tenant == _MERCHANT_A
    assert result_b.metadata.scope.tenant == _MERCHANT_B


async def test_scope_metadata_lists_only_joined_scoped_sources(
    service: CanonicService,
) -> None:
    """``metadata.scope`` reflects sources actually predicated, not the whole policy."""
    # dimensions=["category"] forces a join to the shared `merchants` source; a bare
    # metrics=["revenue"] query never touches order_items/customers/merchants at all.
    # `via=["merchants"]` picks the direct orders->merchants edge over the other path
    # through customers, which would otherwise be ambiguous.
    query = SemanticQuery(metrics=["revenue"], dimensions=["category"], via=["merchants"])
    result = await service.query(query, principal=_VIEWER)

    scope = result.metadata.scope
    assert scope is not None
    assert "orders" in scope.scoped_sources
    # order_items and customers are scoped in the policy but never joined by this
    # query, so they must not appear here.
    assert "order_items" not in scope.scoped_sources
    assert "customers" not in scope.scoped_sources
    assert "merchants" in scope.shared_sources


def test_on_missing_principal_deny_raises_tenant_unresolved(
    service: CanonicService,
) -> None:
    with pytest.raises(TenantUnresolved):
        service.compile_query(SemanticQuery(metrics=["revenue"]), principal=_NO_PRINCIPAL)


# ----------------------------------------------------------------------------
# Metric-level RBAC
# ----------------------------------------------------------------------------


async def test_denied_metric_same_shape_as_missing_metric(service: CanonicService) -> None:
    """``items_sold`` is outside merchant_viewer's allow list — same error as a name
    that doesn't exist at all, so the error channel can't be used as an existence
    oracle (SPEC-E12 §3 stage 1)."""
    with pytest.raises(Unresolved):
        service.compile_query(SemanticQuery(metrics=["items_sold"]), principal=_VIEWER)
    with pytest.raises(Unresolved):
        service.compile_query(SemanticQuery(metrics=["does_not_exist"]), principal=_VIEWER)


async def test_platform_analyst_sees_items_sold_across_merchants(
    service: CanonicService,
) -> None:
    result = await service.query(SemanticQuery(metrics=["items_sold"]), principal=_PLATFORM)
    assert result.result.rows
    assert result.metadata.scope is not None
    assert result.metadata.scope.tenancy_exempt is True


# ----------------------------------------------------------------------------
# Masking and dimensions.deny
# ----------------------------------------------------------------------------


async def test_masking_partial_hides_email_for_admin(service: CanonicService) -> None:
    query = SemanticQuery(metrics=["order_count"], dimensions=["customer_email"])
    result = await service.query(query, principal=_ADMIN)

    assert result.result.rows
    emails = [row[0] for row in result.result.rows]
    assert all(str(e).endswith("***") for e in emails), emails


async def test_dimensions_deny_blocks_email_for_viewer(service: CanonicService) -> None:
    """merchant_viewer's ``dimensions.deny`` rejects the query with the same UNREACHABLE
    shape an undeclared dimension produces, so a denied name is not an existence oracle."""
    query = SemanticQuery(metrics=["order_count"], dimensions=["customer_email"])
    with pytest.raises(Unreachable, match="customer_email"):
        await service.query(query, principal=_VIEWER)


async def test_dimensions_deny_blocks_filtering_on_denied_column(
    service: CanonicService,
) -> None:
    """Filtering on a denied column would let a caller probe values it may not select,
    whether addressed by dimension name, raw column name or table qualifier."""
    for predicate in (
        "customer_email = 'x'",
        "customers.customer_email LIKE 'a%'",
    ):
        query = SemanticQuery(metrics=["order_count"], filters=[predicate])
        with pytest.raises(Unreachable, match="customer_email"):
            await service.query(query, principal=_VIEWER)


async def test_dimensions_deny_allows_filtering_on_permitted_column(
    service: CanonicService,
) -> None:
    query = SemanticQuery(metrics=["order_count"], filters=["status = 'completed'"])
    result = await service.query(query, principal=_VIEWER)
    assert result.result.rows


async def test_admin_can_filter_on_column_viewer_is_denied(service: CanonicService) -> None:
    query = SemanticQuery(metrics=["order_count"], filters=["customer_email LIKE 'a%'"])
    result = await service.query(query, principal=_ADMIN)
    assert result.result.rows


async def test_related_siblings_omit_metrics_outside_policy(tmp_path: Path) -> None:
    """A metric the role may not query must not surface as a sibling suggestion either."""
    root = _build_project(tmp_path / "project")
    roles_path = root / "contracts" / "policies" / "roles.yaml"
    yaml = YAML()
    data = yaml.load(roles_path.read_text())
    data["roles"]["merchant_viewer"]["metrics"]["allow"] = ["revenue"]
    with roles_path.open("w") as f:
        yaml.dump(data, f)
    narrowed = CanonicService.from_project(root)

    query = SemanticQuery(metrics=["revenue"])
    viewer = await narrowed.query(query, principal=_VIEWER)
    platform = await narrowed.query(query, principal=_PLATFORM)

    assert viewer.metadata.related is not None
    assert platform.metadata.related is not None
    viewer_siblings = {m.name for m in viewer.metadata.related.sibling_metrics}
    platform_siblings = {m.name for m in platform.metadata.related.sibling_metrics}
    assert "order_count" not in viewer_siblings
    assert "order_count" in platform_siblings


async def test_dimensions_deny_hidden_from_viewer_discovery(service: CanonicService) -> None:
    viewer_dims = {
        d.name for d in service.describe_metric("order_count", principal=_VIEWER).dimensions
    }
    admin_dims = {
        d.name for d in service.describe_metric("order_count", principal=_ADMIN).dimensions
    }

    assert "customer_email" not in viewer_dims
    assert "customer_phone" not in viewer_dims
    assert "customer_email" in admin_dims


# ----------------------------------------------------------------------------
# The undeclared-source policy hole
# ----------------------------------------------------------------------------


def test_undeclared_source_warns_by_default(service: CanonicService) -> None:
    """Shipped policy uses ``undeclared_source: warn``: a query reaching the
    undeclared ``promotions`` source is still served, with a compile warning."""
    result = service.compile_query(
        SemanticQuery(metrics=["revenue"], dimensions=["promo_name"]), principal=_VIEWER
    )
    assert any("promotions" in w for w in result.warnings), result.warnings


@pytest.fixture
def deny_undeclared_project(tmp_path: Path) -> Path:
    """A private copy with ``undeclared_source: deny`` instead of the shipped
    ``warn`` — proves the fail-closed path still works even though the example
    ships the friendlier mode (see contracts/policies/tenancy.yaml's own comment)."""
    root = _build_project(tmp_path / "project")
    policy_path = root / "contracts" / "policies" / "tenancy.yaml"
    yaml = YAML()
    data = yaml.load(policy_path.read_text())
    data["undeclared_source"] = "deny"
    with policy_path.open("w") as f:
        yaml.dump(data, f)
    return root


def test_undeclared_source_deny_raises_tenant_scope_missing(
    deny_undeclared_project: Path,
) -> None:
    strict_service = CanonicService.from_project(deny_undeclared_project)
    with pytest.raises(TenantScopeMissing):
        strict_service.compile_query(
            SemanticQuery(metrics=["revenue"], dimensions=["promo_name"]), principal=_VIEWER
        )


# ----------------------------------------------------------------------------
# run_sql's two independent gates
# ----------------------------------------------------------------------------


async def test_run_sql_forbidden_role_gate(service: CanonicService) -> None:
    """merchant_viewer has run_sql: false — refused before any connector opens."""
    with pytest.raises(TenantForbidden, match="run_sql"):
        await service.run_sql("SELECT 1", principal=_VIEWER)


async def test_run_sql_forbidden_rls_gate(service: CanonicService) -> None:
    """merchant_admin has run_sql: true, but the shipped connection carries
    rls_enforced: false — still refused, naming the connection."""
    with pytest.raises(TenantForbidden, match="rls_enforced"):
        await service.run_sql("SELECT 1", principal=_ADMIN)


async def test_run_sql_allowed_once_rls_attested(rls_enforced_service: CanonicService) -> None:
    result = await rls_enforced_service.run_sql("SELECT 1", principal=_ADMIN)
    assert result.rows


async def test_run_sql_tenancy_exempt_bypasses_rls_gate(service: CanonicService) -> None:
    """platform_analyst is tenancy_exempt — allowed even though the unmodified
    project's connection still carries rls_enforced: false."""
    result = await service.run_sql("SELECT 1", principal=_PLATFORM)
    assert result.rows


# ----------------------------------------------------------------------------
# CLI surfaces
# ----------------------------------------------------------------------------


def test_cli_tenant_override_warns(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    result = CliRunner().invoke(app, ["query", "--metrics", "revenue", "--tenant", _MERCHANT_A])
    assert result.exit_code == 0, result.output
    assert "overrides the caller's principal" in result.output


def test_mcp_start_http_refuses_tenant_flag(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        app,
        ["mcp", "start", "--transport", "http", "--tenant", _MERCHANT_A],
    )
    assert result.exit_code == 1
    assert "refused on --transport http" in result.output


# ----------------------------------------------------------------------------
# Knowledge scoping
# ----------------------------------------------------------------------------


def test_knowledge_allow_tags_hides_platform_only_pages(
    service: CanonicService,
) -> None:
    viewer_result = service.search_knowledge("margin", principal=_VIEWER)
    platform_result = service.search_knowledge("margin", principal=_PLATFORM)

    assert not any("platform-margin-notes" in hit.page for hit in viewer_result.hits)
    assert any("platform-margin-notes" in hit.page for hit in platform_result.hits)
