"""Tests for `canonic knowledge add`/`search` (SPEC-E3 §5, SPEC-E6).

Fetch and extraction are stubbed (no network, no LLM) via the same monkeypatch style as
tests/cli/test_ingest.py's `_StubFactory` — this exercises the CLI wiring, preview,
confirmation, and write/round-trip behavior, not the real UrlFetchAdapter/RuntimeExtractionSkill
(those have their own tests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import canonic.cli.commands.knowledge as knowledge_cmd
from canonic.cli.app import app
from canonic.connectors.base import DocEvidence, UsageHint
from canonic.connectors.evidence import RawDoc
from canonic.knowledge.loader import load_knowledge_page

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = """\
version: 1
project:
  name: test-project
"""

_SEMANTICS = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - name: order_id
    type: string
  - name: amount
    type: decimal
measures:
  - name: mrr
    expr: "SUM(amount)"
"""


class _FakeFetchAdapter:
    def __init__(self, docs: list[RawDoc]) -> None:
        self._docs = docs

    async def fetch(self) -> list[RawDoc]:
        return self._docs


class _FakeSkill:
    def __init__(self, *, usage_hint: UsageHint = UsageHint.DEFINITION, topic_refs=None) -> None:
        self._usage_hint = usage_hint
        self._topic_refs = topic_refs or []

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        return DocEvidence(
            source=source,
            title=doc.title,
            body=doc.body,
            usage_hint=self._usage_hint,
            topic_refs=self._topic_refs,
            native_ref=doc.source_ref,
            observed_at=datetime.now(UTC),
        )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    (tmp_path / "semantics").mkdir()
    (tmp_path / "semantics" / "orders.yaml").write_text(_SEMANTICS)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_fetch_or_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test replaces the fetch adapter explicitly; default to an empty-doc no-op."""
    monkeypatch.setattr(knowledge_cmd, "make_extraction_skill", lambda *a, **k: _FakeSkill())


def _stub_adapter(monkeypatch: pytest.MonkeyPatch, docs: list[RawDoc]) -> None:
    monkeypatch.setitem(knowledge_cmd._ADHOC_ADAPTERS, "url", lambda _ref: _FakeFetchAdapter(docs))


def test_yes_writes_page_and_prints_path(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_adapter(
        monkeypatch,
        [RawDoc(source_ref="web:https://x", title="SaaS KPIs", body="MRR explained.")],
    )

    result = CliRunner().invoke(app, ["knowledge", "add", "https://x", "--yes"])

    assert result.exit_code == 0, result.output
    written = project / "knowledge" / "global" / "saas-kpis.md"
    assert written.exists()
    assert "wrote" in result.output  # exact path may line-wrap in the Rich console output
    page = load_knowledge_page(written)
    assert page.body == "MRR explained."


def test_declining_confirmation_writes_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_adapter(monkeypatch, [RawDoc(source_ref="web:https://x", title="KPIs", body="body")])

    result = CliRunner().invoke(app, ["knowledge", "add", "https://x"], input="n\n")

    assert result.exit_code == 0, result.output
    assert not (project / "knowledge").exists()


def test_unknown_type_exits_nonzero_and_lists_known_types(project: Path) -> None:
    result = CliRunner().invoke(
        app, ["knowledge", "add", "https://x", "--type", "confluence", "--yes"]
    )

    assert result.exit_code != 0
    assert "confluence" in result.output
    assert "url" in result.output


def test_multi_doc_fetch_errors_and_suggests_ingest(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_adapter(
        monkeypatch,
        [
            RawDoc(source_ref="web:https://x/1", title="One", body="a"),
            RawDoc(source_ref="web:https://x/2", title="Two", body="b"),
        ],
    )

    result = CliRunner().invoke(app, ["knowledge", "add", "https://x", "--yes"])

    assert result.exit_code != 0
    assert "ingest" in result.output


def test_user_flag_writes_under_user_scope(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_adapter(monkeypatch, [RawDoc(source_ref="web:https://x", title="Note", body="body")])

    result = CliRunner().invoke(app, ["knowledge", "add", "https://x", "--user", "alice", "--yes"])

    assert result.exit_code == 0, result.output
    written = project / "knowledge" / "user" / "alice" / "note.md"
    assert written.exists()
    assert load_knowledge_page(written).scope.value == "user"


def test_resolved_topic_ref_is_linked_unresolved_is_not(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_adapter(monkeypatch, [RawDoc(source_ref="web:https://x", title="KPIs", body="body")])
    monkeypatch.setattr(
        knowledge_cmd,
        "make_extraction_skill",
        lambda *a, **k: _FakeSkill(topic_refs=["mrr", "not_a_real_metric"]),
    )

    result = CliRunner().invoke(app, ["knowledge", "add", "https://x", "--yes"])

    assert result.exit_code == 0, result.output
    assert "not_a_real_metric" in result.output  # surfaced as unresolved in the preview note
    page = load_knowledge_page(project / "knowledge" / "global" / "kpis.md")
    assert page.sl_refs == ["warehouse_pg.orders.mrr"]


def test_custom_slug_overrides_derived_slug(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_adapter(monkeypatch, [RawDoc(source_ref="web:https://x", title="KPIs", body="body")])

    result = CliRunner().invoke(
        app, ["knowledge", "add", "https://x", "--slug", "custom-slug", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert (project / "knowledge" / "global" / "custom-slug.md").exists()


def _write_page(project: Path, rel_path: str, *, summary: str, body: str) -> None:
    p = project / "knowledge" / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\nsummary: "{summary}"\n---\n\n{body}\n')


def test_search_finds_matching_page(project: Path) -> None:
    _write_page(
        project, "global/mrr.md", summary="MRR definition", body="Monthly recurring revenue."
    )
    _write_page(project, "global/weather.md", summary="Weather note", body="Rain and sun.")

    result = CliRunner().invoke(app, ["knowledge", "search", "revenue"])

    assert result.exit_code == 0, result.output
    assert "mrr" in result.output
    assert "weather" not in result.output


def test_search_json_matches_mcp_payload_shape(project: Path) -> None:
    _write_page(
        project, "global/mrr.md", summary="MRR definition", body="Monthly recurring revenue."
    )

    result = CliRunner().invoke(app, ["--json", "knowledge", "search", "revenue"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["hits"][0]["page"] == "mrr"
    assert payload["hits"][0]["matched_on"] == ["lexical"]
    assert payload["caveats"] == []


def test_search_no_knowledge_dir_returns_empty(project: Path) -> None:
    result = CliRunner().invoke(app, ["knowledge", "search", "revenue"])

    assert result.exit_code == 0, result.output
    assert "no hits" in result.output


def test_search_no_project_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["knowledge", "search", "revenue"])

    assert result.exit_code != 0
