"""Tests for MCP tool registration and error mapping (canonic/mcp/server.py).

Tools are called in-memory via the FastMCP Client against the built server so no
transport is needed.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.server.auth.auth import AccessToken

from canonic.config import CanonicConfig
from canonic.contract import CONTRACT_SCHEMA
from canonic.contracts.models import (
    CanonicalRef,
    MetricBinding,
    RolePolicy,
    Status,
    TenancyPolicy,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService  # noqa: TC001
from canonic.mcp.auth import CanonicTokenVerifier, ResolvedToken
from canonic.mcp.server import _principal, build_server
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource


def _ambiguous_service(monkeypatch: pytest.MonkeyPatch) -> CanonicService:
    """Build a CanonicService with two join paths from 'owner' to 'dim'."""
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    owner = SemanticSource(
        name="owner",
        connection="db",
        table="t.owner",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)", additivity="additive")],
        joins=[
            Join(to="hop_a", on="owner.id = hop_a.id", relationship=Relationship.MANY_TO_ONE),
            Join(to="hop_b", on="owner.id = hop_b.id", relationship=Relationship.MANY_TO_ONE),
        ],
    )
    hop_a = SemanticSource(
        name="hop_a",
        connection="db",
        table="t.hop_a",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="dim", on="hop_a.id = dim.id", relationship=Relationship.MANY_TO_ONE)],
    )
    hop_b = SemanticSource(
        name="hop_b",
        connection="db",
        table="t.hop_b",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="dim", on="hop_b.id = dim.id", relationship=Relationship.MANY_TO_ONE)],
    )
    dim = SemanticSource(
        name="dim",
        connection="db",
        table="t.dim",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="region", type="string")],
        dimensions=[Dimension(name="region", column="region")],
    )
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="m",
                canonical=CanonicalRef(source="owner", measure="m"),
                status=Status.ACTIVE,
            )
        ],
        guardrails=[],
    )
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "db"},
            "connections": [
                {
                    "id": "db",
                    "type": "postgres",
                    "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "u"},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
        }
    )
    return CanonicService(config=config, resolver=resolver, sources=[owner, hop_a, hop_b, dim])


def test_build_server_defaults_to_no_auth(canonic_service: CanonicService) -> None:
    """stdio transport builds with no auth layer (AMENDMENT-remote-mcp-transport)."""
    mcp = build_server(canonic_service)
    assert mcp.auth is None


def test_build_server_wires_auth_verifier(canonic_service: CanonicService) -> None:
    """http transport passes its resolved verifier straight through to FastMCP."""
    verifier = CanonicTokenVerifier({"secret-token": ResolvedToken(client_id="alice")})
    mcp = build_server(canonic_service, auth=verifier)
    assert mcp.auth is verifier


@pytest.mark.asyncio
async def test_get_overview(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("get_overview", {})
    data = result.data
    assert "domains" in data
    assert isinstance(data["domains"], list)
    orders = next((g for g in data["domains"] if g["name"] == "orders"), None)
    assert orders is not None
    assert any(m["name"] == "revenue" for m in orders["metrics"])
    assert orders["sample_questions"]


@pytest.mark.asyncio
async def test_get_overview_domain_filter(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("get_overview", {"domain": "orders"})
    data = result.data
    assert len(data["domains"]) == 1
    assert data["domains"][0]["name"] == "orders"


@pytest.mark.asyncio
async def test_get_overview_unknown_domain_empty(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("get_overview", {"domain": "nonexistent"})
    data = result.data
    assert data["domains"] == []


@pytest.mark.asyncio
async def test_describe_metric_includes_examples_field(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "revenue"})
    data = result.data
    assert "examples" in data
    assert isinstance(data["examples"], list)


@pytest.mark.asyncio
async def test_list_metrics(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("list_metrics", {})
    data = result.data
    assert isinstance(data["metrics"], list)
    assert any(m["metric"] == "revenue" for m in data["metrics"])
    revenue = next(m for m in data["metrics"] if m["metric"] == "revenue")
    assert all(isinstance(name, str) for name in revenue["dimensions"])
    assert isinstance(data["dimensions"], list)
    catalog_names = {d["name"] for d in data["dimensions"]}
    assert set(revenue["dimensions"]) <= catalog_names
    order_date = next(d for d in data["dimensions"] if d["name"] == "order_date")
    assert set(order_date) == {"name", "source", "label", "description"}


@pytest.mark.asyncio
async def test_describe_metric(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "revenue"})
    data = result.data
    assert data["metric"] == "revenue"
    assert "grain" in data
    assert "dimensions" in data


@pytest.mark.asyncio
async def test_describe_metric_unresolved_returns_error(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "mrr"})
    data = result.data
    assert data["code"] == "unresolved"
    assert "result" not in data


@pytest.mark.asyncio
async def test_resolve_metric(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "rev"})
    data = result.data
    assert data["metric"] == "revenue"
    assert data["source"] == "orders"


@pytest.mark.asyncio
async def test_resolve_metric_unresolved(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "unknown"})
    data = result.data
    assert data["code"] == "unresolved"


@pytest.mark.asyncio
async def test_compile_query(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["revenue"]}})
    data = result.data
    assert "sql" in data["compiled"]
    assert "SELECT" in data["compiled"]["sql"].upper()
    assert data["metadata"]["resolved"]["metrics"]["revenue"] == "orders.total_revenue"
    assert any(g["id"] == "revenue-excludes-refunds" for g in data["metadata"]["guardrails_fired"])
    assert data["metadata"]["contract_schema"] == CONTRACT_SCHEMA
    # S12: related block is always present
    assert "related" in data["metadata"]
    assert isinstance(data["metadata"]["related"]["unused_dimensions"], list)
    assert isinstance(data["metadata"]["related"]["sibling_metrics"], list)
    # order_count is a sibling on the same source
    sibling_names = [m["name"] for m in data["metadata"]["related"]["sibling_metrics"]]
    assert "order_count" in sibling_names
    assert "revenue" not in sibling_names


@pytest.mark.asyncio
async def test_compile_query_unresolved_metric(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["nonexistent"]}})
    data = result.data
    assert data["code"] == "unresolved"


@pytest.mark.asyncio
async def test_compile_query_ambiguous_join_path_returns_actionable_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _ambiguous_service(monkeypatch)
    mcp = build_server(svc)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "compile_query", {"query": {"metrics": ["m"], "dimensions": ["region"]}}
        )
    data = result.data
    assert data["code"] == "ambiguous_join_path"
    candidates = data["candidates"]
    assert len(candidates) == 2
    vias = {tuple(c["via"]) for c in candidates}
    assert vias == {("hop_a", "dim"), ("hop_b", "dim")}
    for c in candidates:
        assert "via" in c
        assert "route" in c
        assert "joins" in c
        assert c["route"].startswith("owner →")


@pytest.mark.asyncio
async def test_compile_query_via_resolves_ambiguous_join_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _ambiguous_service(monkeypatch)
    mcp = build_server(svc)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "compile_query",
            {"query": {"metrics": ["m"], "dimensions": ["region"], "via": ["hop_a", "dim"]}},
        )
    data = result.data
    assert "compiled" in data
    assert "hop_a" in data["compiled"]["sql"]
    assert "hop_b" not in data["compiled"]["sql"]


class TestPrincipal:
    """``_principal`` (SPEC-E12 §5) — derived exclusively from the verified AccessToken."""

    def test_none_without_access_token(
        self, monkeypatch: pytest.MonkeyPatch, canonic_service: CanonicService
    ) -> None:
        monkeypatch.setattr("canonic.mcp.server.get_access_token", lambda: None)
        assert _principal(canonic_service.resolver) is None

    def test_none_when_no_policy_configured(
        self, monkeypatch: pytest.MonkeyPatch, canonic_service: CanonicService
    ) -> None:
        token = AccessToken(token="t", client_id="alice", scopes=[], claims={"merchant_id": "4711"})
        monkeypatch.setattr("canonic.mcp.server.get_access_token", lambda: token)
        # canonic_service's resolver has no tenancy/role policy loaded.
        assert _principal(canonic_service.resolver) is None

    def test_derives_principal_from_token_claims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resolver = ContractResolver(
            bindings=[],
            guardrails=[],
            tenancy=TenancyPolicy(
                schema="tenancy/v1", claim="merchant_id", scoped_sources=[], shared_sources=[]
            ),
            roles=RolePolicy(schema="roles/v1", claim="roles", roles={}),
        )
        token = AccessToken(
            token="t",
            client_id="merchant-4711-agent",
            scopes=[],
            claims={"merchant_id": "4711", "roles": ["merchant_viewer"]},
        )
        monkeypatch.setattr("canonic.mcp.server.get_access_token", lambda: token)
        principal = _principal(resolver)
        assert principal is not None
        assert principal.tenant == "4711"
        assert principal.roles == ("merchant_viewer",)
