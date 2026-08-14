"""Tests for build_diagnostic_bundle — the ``canonic audit --bundle`` payload."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.config import CanonicConfig, Connection, ProjectConfig
from canonic.contract import CONTRACT_SCHEMA
from canonic.instrumentation.bundle import build_diagnostic_bundle

if TYPE_CHECKING:
    from pathlib import Path


def _write_events(dotcanonic: Path, lines: list[dict[str, object]]) -> None:
    dotcanonic.mkdir(parents=True, exist_ok=True)
    (dotcanonic / "events.jsonl").write_text(
        "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n"
    )


def test_bundle_includes_version_and_schema_info(tmp_path: Path) -> None:
    payload = build_diagnostic_bundle(tmp_path, None, None)
    assert payload["contract_schema"] == CONTRACT_SCHEMA
    assert payload["canonic_version"]
    assert payload["python_version"]
    assert payload["platform"]
    assert "generated_at" in payload


def test_bundle_config_none_when_no_config_carries_error(tmp_path: Path) -> None:
    payload = build_diagnostic_bundle(tmp_path, None, "canonic.yaml: version is required")
    assert payload["config"] is None
    assert payload["config_error"] == "canonic.yaml: version is required"


def test_bundle_redacts_sensitive_param_keys(tmp_path: Path) -> None:
    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        connections=[
            Connection(
                id="wh",
                type="postgres",
                params={"host": "db.internal", "password": "hunter2", "api_key": "sk-live-abc"},
                credentials_ref="env:PGPASSWORD",
            )
        ],
    )
    payload = build_diagnostic_bundle(tmp_path, config, None)
    dumped = json.dumps(payload)
    assert "hunter2" not in dumped
    assert "sk-live-abc" not in dumped
    assert "db.internal" in dumped  # non-sensitive params are preserved for debugging
    assert "env:PGPASSWORD" in dumped  # a reference, never a literal secret


def test_bundle_summarizes_funnel_and_report(tmp_path: Path) -> None:
    _write_events(
        tmp_path / ".canonic",
        [
            {
                "kind": "funnel_milestone",
                "milestone": "setup_started",
                "ts": "2026-01-01T00:00:00+00:00",
            },
            {
                "kind": "served_answer",
                "ts": "2026-01-01T00:00:01+00:00",
                "contract_schema": "2.4",
                "query_hash": "sha256:aaa",
                "compiled_sql_hash": "sha256:bbb",
                "connection": "wh",
                "resolved": {},
                "guardrails_fired": [],
                "finality": None,
                "freshness": [],
                "latency_ms": 42,
                "bytes_scanned": None,
                "error": None,
                "trust_score": None,
                "cache_hit": None,
                "over_limit_blocked": None,
            },
        ],
    )
    payload = build_diagnostic_bundle(tmp_path, None, None)
    assert "setup_started" in payload["funnel"]["milestones"]
    assert payload["report"]["count"] == 1


def test_bundle_empty_project_has_zero_counts(tmp_path: Path) -> None:
    payload = build_diagnostic_bundle(tmp_path, None, None)
    assert payload["report"]["count"] == 0
    assert payload["funnel"]["reached"] == []
