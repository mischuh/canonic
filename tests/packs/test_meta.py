"""Tests for stamping provenance/pack_source onto templated pack content (§2.3)."""

from __future__ import annotations

from ruamel.yaml import YAML

from canonic.packs.meta import stamp_contract_yaml, stamp_knowledge_markdown, stamp_semantic_yaml


def test_stamp_semantic_yaml_adds_meta_block():
    text = "name: widgets\nconnection: c1\n"
    out = stamp_semantic_yaml(text, pack="widgets", version="0.1.0", variant="duckdb")
    data = YAML().load(out)
    assert data["name"] == "widgets"
    assert data["meta"]["provenance"] == "human_curated"
    assert data["meta"]["pack_source"] == {
        "pack": "widgets",
        "version": "0.1.0",
        "variant": "duckdb",
    }


def test_stamp_contract_yaml_adds_top_level_fields():
    text = "metric: widget_count\ncanonical:\n  kind: single\n  source: widgets\n  measure: widget_count\n"
    out = stamp_contract_yaml(text, pack="widgets", version="0.1.0", variant="duckdb")
    data = YAML().load(out)
    assert data["provenance"] == "human_curated"
    assert data["pack_source"]["pack"] == "widgets"


def test_stamp_knowledge_markdown_preserves_body():
    text = "---\nsummary: test\n---\n\nBody text here.\n"
    out = stamp_knowledge_markdown(text, pack="widgets", version="0.1.0", variant="duckdb")
    assert out.endswith("Body text here.\n")
    frontmatter = out.split("---\n")[1]
    data = YAML().load(frontmatter)
    assert data["summary"] == "test"
    assert data["meta"]["provenance"] == "human_curated"
    assert data["meta"]["pack_source"]["variant"] == "duckdb"


def test_stamp_knowledge_markdown_without_fence_is_passthrough():
    text = "no frontmatter here\n"
    assert stamp_knowledge_markdown(text, pack="p", version="1", variant="v") == text
