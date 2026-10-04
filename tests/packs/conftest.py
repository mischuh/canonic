"""Shared fixtures for context-pack tests: a small synthetic "widgets" pack over DuckDB.

Deliberately not the real PostHog pack content (that lives in a separate ``canonic-packs``
repo) — a minimal fixture keeps these tests fast, self-contained, and independent of that
repo's state, while still exercising every mechanism the amendment specifies: templating,
``choose_from``, ``derive``, ``required_tables``, meta stamping, and cross-surface
validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_PACK_YAML = """\
pack: widgets
version: 0.1.0
description: "Test fixture pack."

variants:
  - id: duckdb
    label: "Widgets DuckDB"
    mapping: mappings/duckdb.yaml
    connector: duckdb

params:
  - name: connection_id
    description: "Existing connection id."
    required: true
  - name: table
    default: widgets
  - name: status_filter
    description: "Status to highlight."
    required: true
    choose_from:
      query: >
        SELECT status AS value, count(*) AS n
        FROM main.{{table}}
        GROUP BY 1 ORDER BY 2 DESC
      allow_custom: true
  - name: excluded_regions
    description: "Comma-separated region codes to exclude. Leave empty to skip."
    default: ""
    validate: { pattern: '^[a-z]+(,[a-z]+)*$' }
  - name: region_filter
    derive:
      from: excluded_regions
      per_value: "region != '{{value}}'"
      join: " AND "
      when_empty: "TRUE"

required_tables:
  - "main.{{table}}"

provides:
  semantics: [models/widgets.yaml]
  contracts:
    metrics: [metrics/widget_count.yaml]
    guardrails: [guardrails/exclude-regions.yaml]
  knowledge: [knowledge/widget-notes.md]

first_answer:
  metric: widget_count
"""

_MODEL_YAML = """\
name: widgets
connection: "{{connection_id}}"
table: "{{table}}"
grain: [id]
description: "Test widgets model."

columns:
  - { name: id,         type: string,    nullable: false }
  - { name: status,     type: string,    nullable: false }
  - { name: region,     type: string,    nullable: false }
  - { name: created_at, type: timestamp, nullable: false }

measures:
  - name: widget_count
    expr: "count(*)"
    additivity: additive

dimensions:
  - { name: status,      column: status }
  - { name: widget_date, column: created_at, granularity: day }

meta:
  provenance: human_curated
"""

_METRIC_YAML = """\
metric: widget_count
canonical:
  kind: single
  source: widgets
  measure: widget_count
provenance: human_curated
status: active
"""

_GUARDRAIL_YAML = """\
id: widgets-exclude-regions
applies_to: { source: widgets }
kind: mandatory_filter
filter: "{{region_filter}}"
severity: warn
rationale: "Test guardrail excluding named regions."
"""

_KNOWLEDGE_MD = """\
---
summary: "Test knowledge page about widgets."
tags: [test]
sl_refs:
  - "{{connection_id}}.widgets"
usage_mode: reference
---

Widgets are a test fixture entity, not a real product concept.
"""


def write_fixture_pack(pack_root: Path) -> Path:
    """Write the "widgets" fixture pack under ``pack_root/packs/widgets`` and return its dir."""
    pack_dir = pack_root / "packs" / "widgets"
    (pack_dir / "mappings").mkdir(parents=True, exist_ok=True)
    (pack_dir / "models").mkdir(exist_ok=True)
    (pack_dir / "metrics").mkdir(exist_ok=True)
    (pack_dir / "guardrails").mkdir(exist_ok=True)
    (pack_dir / "knowledge").mkdir(exist_ok=True)

    (pack_dir / "pack.yaml").write_text(_PACK_YAML)
    (pack_dir / "mappings" / "duckdb.yaml").write_text("source: test\n")
    (pack_dir / "models" / "widgets.yaml").write_text(_MODEL_YAML)
    (pack_dir / "metrics" / "widget_count.yaml").write_text(_METRIC_YAML)
    (pack_dir / "guardrails" / "exclude-regions.yaml").write_text(_GUARDRAIL_YAML)
    (pack_dir / "knowledge" / "widget-notes.md").write_text(_KNOWLEDGE_MD)
    return pack_dir


_VARIANT_PACK_YAML = """\
pack: widgets_variants
version: 0.1.0
description: "Fixture pack whose variants install different files."

variants:
  - id: a
    label: "Variant A"
    mapping: mappings/a.yaml
    connector: duckdb
    provides:
      semantics: [models/a/widgets.yaml]
  - id: b
    label: "Variant B"
    mapping: mappings/b.yaml
    connector: duckdb
    provides:
      semantics: [models/b/widgets.yaml, models/b/widgets_extra.yaml]
    required_tables: []
    params:
      - name: table
        description: "Table, as variant B names it."
        default: widgets
      - name: note
        default: "only in b"

params:
  - name: connection_id
    required: true
  - name: table
    description: "Table, as the pack names it."
    default: widgets

required_tables:
  - "main.{{table}}"

provides:
  contracts:
    metrics: [metrics/widget_count.yaml]
  knowledge: [knowledge/widget-notes.md]
"""


def write_variant_pack(pack_root: Path) -> Path:
    """Write the "widgets_variants" fixture pack under ``pack_root/packs/widgets_variants``.

    Both variants bind to DuckDB and name the same source ``widgets``, with different
    descriptions. Variant ``b`` also installs a second source, ``widgets_extra``.
    """
    pack_dir = pack_root / "packs" / "widgets_variants"
    for sub in ("mappings", "models/a", "models/b", "metrics", "knowledge"):
        (pack_dir / sub).mkdir(parents=True, exist_ok=True)

    (pack_dir / "pack.yaml").write_text(_VARIANT_PACK_YAML)
    (pack_dir / "mappings" / "a.yaml").write_text("source: test\n")
    (pack_dir / "mappings" / "b.yaml").write_text("source: test\n")
    (pack_dir / "models" / "a" / "widgets.yaml").write_text(_MODEL_YAML)
    (pack_dir / "models" / "b" / "widgets.yaml").write_text(
        _MODEL_YAML.replace("Test widgets model.", "Variant b widgets model.")
    )
    (pack_dir / "models" / "b" / "widgets_extra.yaml").write_text(
        _MODEL_YAML.replace("name: widgets\n", "name: widgets_extra\n", 1)
    )
    (pack_dir / "metrics" / "widget_count.yaml").write_text(_METRIC_YAML)
    (pack_dir / "knowledge" / "widget-notes.md").write_text(_KNOWLEDGE_MD)
    return pack_dir


def write_unreadable_pack(pack_root: Path) -> Path:
    """Write a pack under ``pack_root/packs/future`` whose manifest this canonic cannot read.

    It carries a field that no release knows, which is what a manifest written for a newer
    canonic looks like to an older one.
    """
    pack_dir = pack_root / "packs" / "future"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.yaml").write_text(
        "pack: future\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Future\n"
        "    mapping: mappings/duckdb.yaml\n"
        "    from_a_newer_canonic: true\n"
        "provides: {}\n"
    )
    return pack_dir


@pytest.fixture
def variant_repo(tmp_path: Path) -> Path:
    """A pack repo (plain directory) containing the "widgets_variants" fixture pack."""
    repo_dir = tmp_path / "repo"
    write_variant_pack(repo_dir)
    return repo_dir


@pytest.fixture
def variant_pack_dir(variant_repo: Path) -> Path:
    return variant_repo / "packs" / "widgets_variants"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A pack repo (plain directory) containing the "widgets" fixture pack."""
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    return repo_dir


@pytest.fixture
def fixture_pack_dir(fixture_repo: Path) -> Path:
    return fixture_repo / "packs" / "widgets"


@pytest.fixture
def broken_ref_pack(tmp_path: Path) -> Path:
    """A pack whose guardrail references a semantic source name that does not exist — a
    cross-surface E15 defect ``validate_pack`` must catch (no connection needed)."""
    pack_dir = tmp_path / "broken_ref"
    (pack_dir / "models").mkdir(parents=True)
    (pack_dir / "guardrails").mkdir()
    (pack_dir / "pack.yaml").write_text(
        "pack: broken_ref\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Broken\n"
        "    mapping: mappings/duckdb.yaml\n"
        "params:\n"
        "  - name: connection_id\n"
        "    required: true\n"
        "  - name: table\n"
        "    default: widgets\n"
        "provides:\n"
        "  semantics: [models/widgets.yaml]\n"
        "  contracts:\n"
        "    guardrails: [guardrails/bad.yaml]\n"
    )
    (pack_dir / "models" / "widgets.yaml").write_text(_MODEL_YAML)
    (pack_dir / "guardrails" / "bad.yaml").write_text(
        "id: broken-guardrail\n"
        "applies_to: { source: does_not_exist }\n"
        "kind: mandatory_filter\n"
        'filter: "TRUE"\n'
        "severity: warn\n"
        'rationale: "test"\n'
    )
    return pack_dir


@pytest.fixture
def broken_template_pack(tmp_path: Path) -> Path:
    """A pack whose ``required_tables`` references an undeclared ``{{param}}`` — a
    template defect ``validate_pack`` must catch without touching a connection (the one
    thing plain ``install_pack`` alone would never see, since it never renders
    ``required_tables``)."""
    pack_dir = tmp_path / "broken_template"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "pack: broken_template\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Broken\n"
        "    mapping: mappings/duckdb.yaml\n"
        "params:\n"
        "  - name: connection_id\n"
        "    required: true\n"
        "required_tables:\n"
        '  - "{{undeclared_param}}"\n'
        "provides: {}\n"
    )
    return pack_dir


@pytest.fixture
def widgets_duckdb(tmp_path: Path) -> Iterator[Path]:
    """A DuckDB file seeded with a ``widgets`` table matching the fixture pack's model."""
    db_path = tmp_path / "widgets.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE widgets (id VARCHAR, status VARCHAR, region VARCHAR, created_at TIMESTAMP);"
        "INSERT INTO widgets VALUES "
        "('1', 'active', 'us', '2026-09-01 00:00:00'),"
        "('2', 'active', 'eu', '2026-09-02 00:00:00'),"
        "('3', 'inactive', 'apac', '2026-09-03 00:00:00');"
    )
    con.close()
    yield db_path
