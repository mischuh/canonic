"""Tests for pack.yaml manifest parsing and cross-field validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from canonic.exc import PackError
from canonic.packs.loader import find_pack_dir, load_pack_manifest


def test_load_fixture_manifest(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    assert manifest.pack == "widgets"
    assert manifest.version == "0.1.0"
    assert [v.id for v in manifest.variants] == ["duckdb"]
    assert manifest.variants[0].connector == "duckdb"
    assert manifest.first_answer is not None
    assert manifest.first_answer.metric == "widget_count"


def test_find_pack_dir(fixture_repo, fixture_pack_dir):
    assert find_pack_dir(fixture_repo, "widgets") == fixture_pack_dir


def test_find_pack_dir_unknown_pack_lists_available(fixture_repo):
    with pytest.raises(PackError, match="not found.*widgets"):
        find_pack_dir(fixture_repo, "nope")


def test_manifest_not_found(tmp_path):
    with pytest.raises(PackError, match="pack manifest not found"):
        load_pack_manifest(tmp_path)


def test_variant_lookup(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    variant = manifest.variant("duckdb")
    assert variant.label == "Widgets DuckDB"
    with pytest.raises(PackError, match="unknown variant"):
        manifest.variant("bigquery")


def test_param_lookup(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    assert manifest.param("table").default == "widgets"
    assert manifest.param("nonexistent") is None


def test_choose_from_requires_exactly_one_of_query_or_same_as():
    from canonic.packs.manifest import ChooseFrom

    with pytest.raises(ValidationError, match="exactly one"):
        ChooseFrom()
    with pytest.raises(ValidationError, match="exactly one"):
        ChooseFrom(query="SELECT 1", same_as="other")


def test_manifest_rejects_duplicate_variant_ids():
    from canonic.packs.manifest import PackManifest

    with pytest.raises(ValidationError, match="duplicate variant id"):
        PackManifest.model_validate(
            {
                "pack": "x",
                "version": "0.1.0",
                "variants": [
                    {"id": "a", "label": "A", "mapping": "m.yaml"},
                    {"id": "a", "label": "A2", "mapping": "m2.yaml"},
                ],
                "provides": {},
            }
        )


def test_manifest_rejects_unknown_derive_source():
    from canonic.packs.manifest import PackManifest

    with pytest.raises(ValidationError, match="derive.from references unknown param"):
        PackManifest.model_validate(
            {
                "pack": "x",
                "version": "0.1.0",
                "variants": [{"id": "a", "label": "A", "mapping": "m.yaml"}],
                "params": [
                    {"name": "derived", "derive": {"from": "nope", "per_value": "{{value}}"}},
                ],
                "provides": {},
            }
        )


def test_manifest_requires_at_least_one_variant():
    from canonic.packs.manifest import PackManifest

    with pytest.raises(ValidationError, match="at least one variant"):
        PackManifest.model_validate(
            {"pack": "x", "version": "0.1.0", "variants": [], "provides": {}}
        )
