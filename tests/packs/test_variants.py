"""Variants that carry their own files, params and required tables
(AMENDMENT-pack-variant-content).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from canonic.config import scaffold_project
from canonic.exc import PackError
from canonic.packs.install import install_pack, resolve_params
from canonic.packs.loader import load_pack_manifest
from canonic.packs.manifest import PackManifest
from canonic.semantic.loader import list_semantic_sources

_VARIANT_A = {"id": "a", "label": "A", "mapping": "m.yaml"}


def _manifest(variant_b: dict[str, Any] | None = None, **extra: Any) -> PackManifest:
    """A pack with shared content and two variants, ``b`` carrying ``variant_b``."""
    spec: dict[str, Any] = {
        "pack": "x",
        "version": "0.1.0",
        "variants": [
            _VARIANT_A,
            {"id": "b", "label": "B", "mapping": "m.yaml", **(variant_b or {})},
        ],
        "params": [
            {"name": "connection_id", "required": True},
            {"name": "table", "default": "t"},
        ],
        "required_tables": ["{{table}}"],
        "provides": {
            "semantics": ["models/shared.yaml"],
            "contracts": {"metrics": ["metrics/m.yaml"], "guardrails": ["guardrails/g.yaml"]},
            "knowledge": ["knowledge/k.md"],
        },
        **extra,
    }
    return PackManifest.model_validate(spec)


def test_for_variant_adds_the_variants_paths_after_the_pack_wide_ones():
    manifest = _manifest(
        {
            "provides": {
                "semantics": ["models/b/m.yaml"],
                "contracts": {"metrics": ["metrics/b.yaml"], "guardrails": ["guardrails/b.yaml"]},
                "knowledge": ["knowledge/b.md"],
            }
        }
    )

    resolved = manifest.for_variant("b").provides

    assert resolved.semantics == ["models/shared.yaml", "models/b/m.yaml"]
    assert resolved.contracts.metrics == ["metrics/m.yaml", "metrics/b.yaml"]
    assert resolved.contracts.guardrails == ["guardrails/g.yaml", "guardrails/b.yaml"]
    assert resolved.knowledge == ["knowledge/k.md", "knowledge/b.md"]


def test_for_variant_leaves_other_variants_out():
    manifest = _manifest({"provides": {"semantics": ["models/b/m.yaml"]}})

    assert manifest.for_variant("a").provides.semantics == ["models/shared.yaml"]


def test_for_variant_without_own_content_keeps_the_pack_wide_values():
    manifest = _manifest()

    resolved = manifest.for_variant("b")

    assert resolved.provides == manifest.provides
    assert resolved.params == manifest.params
    assert resolved.required_tables == manifest.required_tables


def test_for_variant_replaces_required_tables_when_the_variant_sets_them():
    manifest = _manifest({"required_tables": ["{{table}}_b", "other"]})

    assert manifest.for_variant("b").required_tables == ["{{table}}_b", "other"]
    assert manifest.for_variant("a").required_tables == ["{{table}}"]


def test_for_variant_with_an_empty_required_tables_list_requires_none():
    manifest = _manifest({"required_tables": []})

    assert manifest.for_variant("b").required_tables == []


def test_for_variant_replaces_a_param_in_place_and_appends_new_ones():
    manifest = _manifest(
        {
            "params": [
                {"name": "extra", "default": "e"},
                {"name": "table", "default": "t_b"},
            ]
        }
    )

    params = manifest.for_variant("b").params

    assert [p.name for p in params] == ["connection_id", "table", "extra"]
    assert params[1].default == "t_b"


def test_for_variant_declares_only_the_chosen_variant_with_its_content_cleared():
    manifest = _manifest({"provides": {"semantics": ["models/b/m.yaml"]}, "required_tables": []})

    (variant,) = manifest.for_variant("b").variants

    assert variant.id == "b"
    assert variant.provides is None
    assert variant.required_tables is None
    assert variant.params == []


def test_for_variant_is_idempotent():
    manifest = _manifest(
        {
            "provides": {"semantics": ["models/b/m.yaml"]},
            "required_tables": ["x"],
            "params": [{"name": "extra", "default": "e"}],
        }
    )

    once = manifest.for_variant("b")

    assert once.for_variant("b") == once


def test_for_variant_unknown_id_lists_the_known_ones():
    with pytest.raises(PackError, match="unknown variant 'c'.*known: a, b"):
        _manifest().for_variant("c")


def test_variant_rejects_an_unknown_field():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _manifest({"provide": {"semantics": []}})


def test_a_path_listed_at_pack_level_and_in_a_variant_is_rejected():
    with pytest.raises(ValidationError, match="variant 'b': path 'models/shared.yaml'"):
        _manifest({"provides": {"semantics": ["models/shared.yaml"]}})


def test_a_path_listed_twice_in_one_variant_is_rejected():
    with pytest.raises(ValidationError, match="variant 'b': path 'models/b.yaml'"):
        _manifest({"provides": {"semantics": ["models/b.yaml", "models/b.yaml"]}})


def test_a_path_listed_twice_at_pack_level_is_rejected():
    with pytest.raises(ValidationError, match="path 'metrics/m.yaml' is listed more than once"):
        _manifest(
            provides={"contracts": {"metrics": ["metrics/m.yaml", "metrics/m.yaml"]}},
        )


def test_two_knowledge_files_with_one_name_are_rejected():
    with pytest.raises(ValidationError, match="variant 'b': knowledge files share the file name"):
        _manifest({"provides": {"knowledge": ["knowledge/b/k.md"]}})


def test_a_variant_param_with_an_unknown_derive_source_is_rejected():
    with pytest.raises(ValidationError, match="variant 'b': param 'derived' derive.from"):
        _manifest(
            {
                "params": [
                    {"name": "derived", "derive": {"from": "nope", "per_value": "{{value}}"}},
                ]
            }
        )


def test_a_variant_param_may_derive_from_a_pack_wide_param():
    manifest = _manifest(
        {
            "params": [
                {"name": "derived", "derive": {"from": "table", "per_value": "{{value}}"}},
            ]
        }
    )

    assert [p.name for p in manifest.for_variant("b").params][-1] == "derived"


def test_a_variant_that_lists_a_param_twice_is_rejected():
    with pytest.raises(ValidationError, match="variant 'b': duplicate param name"):
        _manifest({"params": [{"name": "extra"}, {"name": "extra"}]})


def test_the_fixture_pack_loads_with_variant_content(variant_pack_dir):
    manifest = load_pack_manifest(variant_pack_dir)

    assert [v.id for v in manifest.variants] == ["a", "b"]
    assert manifest.for_variant("a").provides.semantics == ["models/a/widgets.yaml"]
    assert manifest.for_variant("b").provides.semantics == [
        "models/b/widgets.yaml",
        "models/b/widgets_extra.yaml",
    ]
    assert manifest.for_variant("b").required_tables == []
    assert manifest.for_variant("a").required_tables == ["main.{{table}}"]


def _install(tmp_path, pack_dir, variant_id):
    root = tmp_path / f"project_{variant_id}"
    scaffold_project(root)
    manifest = load_pack_manifest(pack_dir)
    variant = manifest.variant(variant_id)
    params = resolve_params(manifest.for_variant(variant_id), {"connection_id": "widgets_db"})
    result = install_pack(root, pack_dir, manifest, variant, params)
    return root, result


def test_install_writes_the_shared_files_and_only_the_chosen_variants(tmp_path, variant_pack_dir):
    root_a, result_a = _install(tmp_path, variant_pack_dir, "a")
    root_b, result_b = _install(tmp_path, variant_pack_dir, "b")

    assert result_a.validation_errors == []
    assert result_b.validation_errors == []
    assert len(result_a.written) == 3
    assert len(result_b.written) == 4
    assert [s.name for s in list_semantic_sources(root_a)] == ["widgets"]
    assert sorted(s.name for s in list_semantic_sources(root_b)) == ["widgets", "widgets_extra"]
    assert (root_a / "contracts" / "metrics" / "widget_count.yaml").exists()
    assert (root_b / "contracts" / "metrics" / "widget_count.yaml").exists()


def test_two_variants_install_different_content_at_the_same_path(tmp_path, variant_pack_dir):
    root_a, _ = _install(tmp_path, variant_pack_dir, "a")
    root_b, _ = _install(tmp_path, variant_pack_dir, "b")

    text_a = (root_a / "semantics" / "widgets_db" / "widgets.yaml").read_text()
    text_b = (root_b / "semantics" / "widgets_db" / "widgets.yaml").read_text()

    assert "Test widgets model." in text_a
    assert "Variant b widgets model." in text_b


def test_installed_files_carry_the_chosen_variant_in_pack_source(tmp_path, variant_pack_dir):
    root, _ = _install(tmp_path, variant_pack_dir, "b")

    sources = list_semantic_sources(root)

    assert {s.meta.pack_source.variant for s in sources} == {"b"}


def test_install_accepts_a_manifest_that_was_already_resolved(tmp_path, variant_pack_dir):
    root = tmp_path / "project"
    scaffold_project(root)
    manifest = load_pack_manifest(variant_pack_dir)
    variant = manifest.variant("b")
    resolved = manifest.for_variant("b")
    params = resolve_params(resolved, {"connection_id": "widgets_db"})

    result = install_pack(root, variant_pack_dir, resolved, variant, params)

    assert len(result.written) == 4


def test_install_rejects_two_files_that_write_the_same_target(tmp_path, variant_pack_dir):
    """Both model files declare ``name: widgets``, so the second would replace the first."""
    pack_yaml = variant_pack_dir / "pack.yaml"
    pack_yaml.write_text(
        pack_yaml.read_text().replace(
            "semantics: [models/a/widgets.yaml]",
            "semantics: [models/a/widgets.yaml, models/b/widgets.yaml]",
        )
    )
    root = tmp_path / "project"
    scaffold_project(root)
    manifest = load_pack_manifest(variant_pack_dir)
    variant = manifest.variant("a")
    params = resolve_params(manifest.for_variant("a"), {"connection_id": "widgets_db"})

    with pytest.raises(
        PackError,
        match=r"models/b/widgets\.yaml and models/a/widgets\.yaml both write "
        r"semantics/widgets_db/widgets\.yaml",
    ):
        install_pack(root, variant_pack_dir, manifest, variant, params)

    kept = (root / "semantics" / "widgets_db" / "widgets.yaml").read_text()
    assert "Test widgets model." in kept
