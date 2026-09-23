"""Identity assertion as a fourth Principal source (AMENDMENT-e12-identity-assertion-principal).

Runs real queries against a two-tenant DuckDB project, so tenant scoping is checked on
the returned rows and not only on the compiled SQL. The verified ``AccessToken`` is
injected the same way ``tests/mcp/test_server.py`` does it. The exchange that mints an
identity-asserted token is covered in ``tests/mcp/test_auth.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.server.auth.auth import AccessToken
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.contracts.principal import Principal
from canonic.core.service import CanonicService
from canonic.mcp.auth import ASSERTED_CLAIMS_KEY, ID_JAG_GRANT
from canonic.mcp.server import _effective_user, build_server
from tests.conftest import MCP_ERAS, mcp_client

if TYPE_CHECKING:
    from pathlib import Path

_EMPLOYEE = "jsmith@acme-corp.com"
_AGENT = "agent-scheduled-reports"
_VIEWER_CLAIMS: dict[str, Any] = {"merchant_id": "4711", "roles": ["merchant_viewer"]}


@pytest.fixture
def tenant_project(tmp_path: Path) -> Path:
    """Orders for merchants 4711 and 4712, tenant-scoped on ``merchant_id``.

    ``merchant_viewer`` may read ``revenue`` but not ``order_count``, so role filtering
    is observable alongside tenant scoping.
    """
    import duckdb

    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, merchant_id VARCHAR, amount DECIMAL(12,2));"
        "INSERT INTO orders VALUES (1, '4711', 100.00), (2, '4711', 50.00), (3, '4712', 999.00);"
    )
    con.close()

    (tmp_path / "canonic.yaml").write_text(
        "version: 1\n"
        "project:\n  name: test\n  default_connection: warehouse_duckdb\n"
        "connections:\n"
        f"  - id: warehouse_duckdb\n    type: duckdb\n    params: {{path: {db_path}}}\n"
        "llm:\n  provider: openai_compatible\n  base_url: http://localhost/v1\n  model: llama3\n"
    )
    sem = tmp_path / "semantics" / "warehouse_duckdb"
    sem.mkdir(parents=True)
    (sem / "orders.yaml").write_text(
        "name: orders\nconnection: warehouse_duckdb\ntable: orders\ngrain: [order_id]\n"
        "columns:\n  - {name: order_id, type: int, nullable: false}\n"
        "  - {name: merchant_id, type: string, nullable: false}\n"
        "  - {name: amount, type: decimal, nullable: false}\n"
        "measures:\n  - {name: total_revenue, expr: 'sum(amount)', additivity: additive}\n"
        "  - {name: order_count, expr: 'count(order_id)', additivity: additive}\n"
    )
    metrics = tmp_path / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "revenue.yaml").write_text(
        "metric: revenue\ncanonical:\n  source: orders\n  measure: total_revenue\nstatus: active\n"
    )
    (metrics / "order_count.yaml").write_text(
        "metric: order_count\ncanonical:\n  source: orders\n  measure: order_count\n"
        "status: active\n"
    )
    policies = tmp_path / "contracts" / "policies"
    policies.mkdir(parents=True)
    (policies / "tenancy.yaml").write_text(
        "schema: tenancy/v1\nclaim: merchant_id\non_missing_principal: deny\n"
        "scoped_sources:\n  - { source: orders, column: merchant_id }\n"
    )
    (policies / "roles.yaml").write_text(
        "schema: roles/v1\nclaim: roles\ndefault_role: merchant_viewer\nroles:\n"
        "  merchant_viewer:\n    metrics: { allow: [revenue] }\n"
    )
    return tmp_path


def _interactive_token(claims: dict[str, Any]) -> AccessToken:
    """An ``OIDCProxy`` login token: claims at the top level, subject and caller alike."""
    return AccessToken(token="t", client_id=_EMPLOYEE, scopes=[], claims=dict(claims))


def _asserted_token(asserted: dict[str, Any]) -> AccessToken:
    """A token ``CanonicOIDCProxy`` mints from an ID-JAG for ``_EMPLOYEE``, presented by ``_AGENT``."""
    return AccessToken(
        token="t",
        client_id=_AGENT,
        scopes=[],
        subject=_EMPLOYEE,
        claims={"fastmcp_grant": ID_JAG_GRANT, "sub": _EMPLOYEE, ASSERTED_CLAIMS_KEY: asserted},
    )


async def _call(
    service: CanonicService,
    token: AccessToken,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    args: dict[str, Any],
    era: str = "sessionless",
) -> Any:
    monkeypatch.setattr("canonic.mcp.server.get_access_token", lambda: token)
    async with mcp_client(build_server(service), era) as client:
        result = await client.call_tool(tool, args)
    return result.data


def _query(metric: str) -> dict[str, Any]:
    return {"query": {"metrics": [metric]}}


def _events(project: Path) -> list[dict[str, Any]]:
    log_file = project / ".canonic" / "events.jsonl"
    return [json.loads(line) for line in log_file.read_text().splitlines()]


class TestScopedPrincipal:
    """S25: an identity-asserted token scopes a query like an interactive login does."""

    @pytest.mark.parametrize("era", MCP_ERAS)
    async def test_rows_and_scope_match_interactive_login(
        self, tenant_project: Path, monkeypatch: pytest.MonkeyPatch, era: str
    ) -> None:
        service = CanonicService.from_project(tenant_project)
        interactive = await _call(
            service,
            _interactive_token(_VIEWER_CLAIMS),
            monkeypatch,
            "query",
            _query("revenue"),
            era,
        )
        asserted = await _call(
            service, _asserted_token(_VIEWER_CLAIMS), monkeypatch, "query", _query("revenue"), era
        )

        # AC1: scoped to tenant 4711, merchant 4712's order never counted.
        assert [[str(v) for v in row] for row in asserted["result"]["rows"]] == [["150.00"]]
        assert asserted["result"] == interactive["result"]
        # AC2: the compiler cannot tell the two auth paths apart.
        assert asserted["metadata"]["scope"] == interactive["metadata"]["scope"]
        assert asserted["metadata"]["scope"]["tenant"] == "4711"

    async def test_role_filtering_matches_interactive_login(
        self, tenant_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S25 AC1: metrics are filtered per ``merchant_viewer`` on both paths."""
        service = CanonicService.from_project(tenant_project)
        interactive = await _call(
            service,
            _interactive_token(_VIEWER_CLAIMS),
            monkeypatch,
            "query",
            _query("order_count"),
        )
        asserted = await _call(
            service, _asserted_token(_VIEWER_CLAIMS), monkeypatch, "query", _query("order_count")
        )
        assert asserted["code"] == "unresolved"
        assert asserted == interactive


class TestAnswerEventUser:
    """S26: ``AnswerEvent.user`` separates the asserted subject from the calling agent."""

    async def test_asserted_call_logs_subject_and_agent(
        self, tenant_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = CanonicService.from_project(tenant_project)
        await _call(
            service, _asserted_token(_VIEWER_CLAIMS), monkeypatch, "query", _query("revenue")
        )
        event = _events(tenant_project)[-1]
        assert event["user"] == {"subject": _EMPLOYEE, "acted_via": _AGENT}
        assert event["tenant"] == "4711"

    async def test_other_paths_keep_plain_string_user(
        self, tenant_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: no ``acted_via`` outside the assertion path."""
        service = CanonicService.from_project(tenant_project)
        await _call(
            service, _interactive_token(_VIEWER_CLAIMS), monkeypatch, "query", _query("revenue")
        )
        event = _events(tenant_project)[-1]
        assert event["user"] == _EMPLOYEE


class TestFailClosed:
    """S27: an asserted token without the tenancy claim fails like any other."""

    async def test_missing_tenancy_claim_is_tenant_unresolved(
        self, tenant_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = CanonicService.from_project(tenant_project)
        asserted = await _call(
            service,
            _asserted_token({"roles": ["merchant_viewer"]}),
            monkeypatch,
            "query",
            _query("revenue"),
        )
        static = await _call(
            service,
            AccessToken(token="t", client_id="reporting-service", scopes=[], claims={}),
            monkeypatch,
            "query",
            _query("revenue"),
        )
        assert asserted["code"] == "tenant_unresolved"
        assert asserted == static
        # AC1: no SQL emitted.
        assert _events(tenant_project)[-2]["compiled_sql_hash"] is None


@pytest.mark.parametrize("era", MCP_ERAS)
def test_adapter_parity_with_cli_principal(
    tenant_project: Path, monkeypatch: pytest.MonkeyPatch, era: str
) -> None:
    """S28 AC1: an identity-asserted MCP ``query`` returns the same payload as the CLI
    under an equivalent explicitly-supplied principal (``--tenant``, SPEC-E12 §7).

    Synchronous for the same reason as ``test_run_report_parity``: the CLI calls
    ``asyncio.run`` itself.
    """
    monkeypatch.chdir(tenant_project)
    cli = CliRunner().invoke(
        app,
        ["--json", "query", "--metrics", "revenue", "--tenant", "4711"],
        catch_exceptions=False,
    )
    assert cli.exit_code == 0, cli.stdout
    # The --tenant warning precedes the JSON payload on stdout.
    cli_payload = json.loads(cli.stdout.strip().splitlines()[-1])

    service = CanonicService.from_project(tenant_project)
    mcp_payload = asyncio.run(
        _call(
            service, _asserted_token(_VIEWER_CLAIMS), monkeypatch, "query", _query("revenue"), era
        )
    )

    assert json.dumps(mcp_payload, sort_keys=True, default=str) == json.dumps(
        cli_payload, sort_keys=True, default=str
    )


class TestPersonalKnowledgeIdentity:
    """Personal knowledge pages follow the asserted employee, not the calling agent."""

    def test_effective_user_is_asserted_subject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "canonic.mcp.server.get_access_token", lambda: _asserted_token(_VIEWER_CLAIMS)
        )
        principal = Principal(tenant="4711", roles=("merchant_viewer",))
        assert _effective_user(principal, user="someone-else") == _EMPLOYEE

    def test_effective_user_for_other_tokens_is_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = AccessToken(
            token="t", client_id="reporting-service", scopes=[], claims=_VIEWER_CLAIMS
        )
        monkeypatch.setattr("canonic.mcp.server.get_access_token", lambda: token)
        principal = Principal(tenant="4711", roles=("merchant_viewer",))
        assert _effective_user(principal, user="someone-else") == "reporting-service"
