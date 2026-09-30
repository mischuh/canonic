"""Tests for connection-free pack validation (``canonic pack validate``)."""

from __future__ import annotations

from canonic.packs.install import synthesize_params
from canonic.packs.loader import load_pack_manifest
from canonic.packs.validate import validate_pack


def test_synthesize_params_fills_every_param_including_derived(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    params = synthesize_params(manifest)

    assert params["connection_id"] == "example_connection_id"
    assert params["table"] == "widgets"  # its declared default
    assert params["status_filter"] == "example_status_filter"
    assert params["excluded_regions"] == "example"  # forced non-empty: feeds a derive
    assert params["region_filter"] == "region != 'example'"  # per_value actually exercised


def test_synthesize_params_never_violates_a_stricter_validate_pattern(tmp_path):
    """The safe placeholder itself must never break a pack's own declared constraint —
    falls back to the param's default rather than fabricating a non-matching value."""
    pack_dir = tmp_path / "digits_only"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "pack: digits_only\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Digits\n"
        "    mapping: mappings/duckdb.yaml\n"
        "params:\n"
        "  - name: connection_id\n"
        "    required: true\n"
        "  - name: digits\n"
        "    default: ''\n"
        "    validate: { pattern: '^[0-9]+$' }\n"
        "  - name: derived\n"
        "    derive:\n"
        "      from: digits\n"
        "      per_value: 'n={{value}}'\n"
        "      when_empty: 'none'\n"
        "provides: {}\n"
    )
    manifest = load_pack_manifest(pack_dir)

    params = synthesize_params(manifest)

    assert params["digits"] == ""  # "example" fails ^[0-9]+$ — falls back to the default
    assert params["derived"] == "none"


def test_validate_pack_needs_no_connection_or_db(fixture_pack_dir):
    """The whole point: no DuckDB fixture, no Connection object, nothing live at all."""
    manifest = load_pack_manifest(fixture_pack_dir)
    variant = manifest.variant("duckdb")

    result = validate_pack(fixture_pack_dir, manifest, variant)

    assert result.validation_errors == []
    assert len(result.written) == 4


def test_validate_pack_writes_into_a_discarded_scratch_dir(fixture_pack_dir, tmp_path):
    manifest = load_pack_manifest(fixture_pack_dir)
    variant = manifest.variant("duckdb")

    result = validate_pack(fixture_pack_dir, manifest, variant)

    for path in result.written:
        assert not path.exists()  # the temp dir is gone once validate_pack returns
        assert str(path).startswith("/") and "canonic-pack-validate-widgets-" in str(path)
        assert not str(path).startswith(str(tmp_path))  # never touches the test's own dirs


def test_validate_pack_catches_cross_surface_mismatch(broken_ref_pack):
    manifest = load_pack_manifest(broken_ref_pack)
    variant = manifest.variant("duckdb")

    result = validate_pack(broken_ref_pack, manifest, variant)

    assert result.validation_errors
    assert any("does_not_exist" in msg for msg in result.validation_errors)


def test_validate_pack_catches_unresolved_required_tables_template(broken_template_pack):
    manifest = load_pack_manifest(broken_template_pack)
    variant = manifest.variant("duckdb")

    result = validate_pack(broken_template_pack, manifest, variant)

    assert result.validation_errors
    assert any("undeclared_param" in msg for msg in result.validation_errors)


def test_validate_pack_catches_unresolved_choose_from_template(tmp_path):
    """A choose_from.query referencing an undeclared {{param}} — never executed, but the
    template token itself is still checked (install_pack alone never touches choose_from)."""
    pack_dir = tmp_path / "broken_choose_from"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "pack: broken_choose_from\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Broken\n"
        "    mapping: mappings/duckdb.yaml\n"
        "params:\n"
        "  - name: connection_id\n"
        "    required: true\n"
        "  - name: picked\n"
        "    required: true\n"
        "    choose_from:\n"
        '      query: "SELECT x FROM {{undeclared_table_param}}"\n'
        "provides: {}\n"
    )
    manifest = load_pack_manifest(pack_dir)
    variant = manifest.variant("duckdb")

    result = validate_pack(pack_dir, manifest, variant)

    assert result.validation_errors
    assert any("undeclared_table_param" in msg for msg in result.validation_errors)
