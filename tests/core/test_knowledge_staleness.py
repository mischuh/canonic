"""``read_knowledge_page`` attaches a staleness signal past the validation window (SPEC-E6 §8, S8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from canonic.config import CanonicConfig
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_CONFIG = {
    "version": 1,
    "project": {"name": "test", "default_connection": "warehouse_pg"},
    "connections": [
        {
            "id": "warehouse_pg",
            "type": "postgres",
            "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "test"},
            "credentials_ref": "env:PG_PASSWORD",
        }
    ],
    "llm": {"provider": "openai_compatible", "base_url": "http://localhost/v1", "model": "llama3"},
}


def _page(validated_at: datetime | None) -> str:
    meta = (
        "" if validated_at is None else f"meta:\n  last_validated_at: {validated_at.isoformat()}\n"
    )
    return f'---\nsummary: "Revenue."\ntags: [finance]\n{meta}---\n\nRevenue is paid orders.\n'


def _service(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, window_days: int | None = None
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    raw = dict(_CONFIG)
    if window_days is not None:
        raw["knowledge"] = {"staleness_window_days": window_days}
    return CanonicService(
        config=CanonicConfig.model_validate(raw),
        resolver=ContractResolver(bindings=[], guardrails=[]),
        sources=[],
        project_root=root,
    )


def _write(root: Path, validated_at: datetime | None) -> None:
    (root / "knowledge" / "global").mkdir(parents=True)
    (root / "knowledge" / "global" / "revenue.md").write_text(_page(validated_at))


def test_recently_validated_page_has_no_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, datetime.now(UTC) - timedelta(days=3))
    page = _service(tmp_path, monkeypatch).read_knowledge_page("revenue")
    assert page["meta"]["staleness"] is None


def test_page_validated_beyond_window_reports_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, datetime.now(UTC) - timedelta(days=120))
    staleness = _service(tmp_path, monkeypatch).read_knowledge_page("revenue")["meta"]["staleness"]
    assert staleness["age_days"] == 120
    assert "120 days" in staleness["message"]


def test_never_validated_page_reports_null_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, None)
    staleness = _service(tmp_path, monkeypatch).read_knowledge_page("revenue")["meta"]["staleness"]
    assert staleness["age_days"] is None
    assert "never validated" in staleness["message"]


def test_window_comes_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, datetime.now(UTC) - timedelta(days=10))
    default = _service(tmp_path, monkeypatch).read_knowledge_page("revenue")
    tight = _service(tmp_path, monkeypatch, window_days=7).read_knowledge_page("revenue")
    assert default["meta"]["staleness"] is None
    assert tight["meta"]["staleness"]["age_days"] == 10
