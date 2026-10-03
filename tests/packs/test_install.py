"""Tests for the core install routine: resolve_params, check_required_tables, install_pack."""

from __future__ import annotations

import pytest

from canonic.config import Connection, scaffold_project
from canonic.contracts.loader import load_guardrails, load_metric_bindings
from canonic.contracts.validate import validate_contracts
from canonic.exc import PackError
from canonic.knowledge.loader import load_knowledge_page
from canonic.packs.install import check_required_tables, install_pack, resolve_params
from canonic.packs.loader import load_pack_manifest
from canonic.semantic.loader import list_semantic_sources


def _widgets_connection(db_path) -> Connection:
    return Connection(id="widgets_db", type="duckdb", params={"path": str(db_path)})


# --- resolve_params ----------------------------------------------------------


def test_resolve_params_fills_defaults_and_derives(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    params = resolve_params(
        manifest,
        {"connection_id": "widgets_db", "status_filter": "active", "excluded_regions": "eu,apac"},
    )
    assert params["table"] == "widgets"
    assert params["region_filter"] == "region != 'eu' AND region != 'apac'"


def test_resolve_params_explicit_wins_over_derive(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    params = resolve_params(
        manifest,
        {
            "connection_id": "widgets_db",
            "status_filter": "active",
            "region_filter": "TRUE  -- hand-overridden",
        },
    )
    assert params["region_filter"] == "TRUE  -- hand-overridden"


def test_resolve_params_reports_all_missing_required_at_once(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    with pytest.raises(PackError) as excinfo:
        resolve_params(manifest, {})
    message = str(excinfo.value)
    assert "connection_id" in message
    assert "status_filter" in message


def test_resolve_params_validates_pattern(fixture_pack_dir):
    manifest = load_pack_manifest(fixture_pack_dir)
    with pytest.raises(PackError, match="does not match pattern"):
        resolve_params(
            manifest,
            {
                "connection_id": "widgets_db",
                "status_filter": "active",
                "excluded_regions": "EU",  # uppercase violates the fixture's pattern
            },
        )


# --- check_required_tables ----------------------------------------------------


def test_check_required_tables_passes_when_table_exists(fixture_pack_dir, widgets_duckdb):
    manifest = load_pack_manifest(fixture_pack_dir)
    params = resolve_params(manifest, {"connection_id": "widgets_db", "status_filter": "active"})
    check_required_tables(manifest, params, _widgets_connection(widgets_duckdb))  # no raise


def test_check_required_tables_raises_on_missing_table(fixture_pack_dir, widgets_duckdb):
    manifest = load_pack_manifest(fixture_pack_dir)
    params = resolve_params(
        manifest,
        {"connection_id": "widgets_db", "status_filter": "active", "table": "does_not_exist"},
    )
    with pytest.raises(PackError, match="required table.*not found"):
        check_required_tables(manifest, params, _widgets_connection(widgets_duckdb))


# --- install_pack (full round-trip through the real E5/E15/E6 loaders) -------


def test_install_pack_writes_valid_files(tmp_path, fixture_pack_dir, widgets_duckdb):
    root = tmp_path / "project"
    scaffold_project(root)
    manifest = load_pack_manifest(fixture_pack_dir)
    variant = manifest.variant("duckdb")
    params = resolve_params(
        manifest,
        {"connection_id": "widgets_db", "status_filter": "active", "excluded_regions": "eu"},
    )
    connection = _widgets_connection(widgets_duckdb)
    check_required_tables(manifest, params, connection)

    result = install_pack(root, fixture_pack_dir, manifest, variant, params)

    assert result.validation_errors == []
    assert len(result.written) == 4
    assert (root / "semantics" / "widgets_db" / "widgets.yaml") in result.written
    assert (root / "contracts" / "metrics" / "widget_count.yaml") in result.written
    assert (root / "contracts" / "guardrails" / "widgets-exclude-regions.yaml") in result.written
    assert (root / "knowledge" / "global" / "widget-notes.md") in result.written

    # Re-parse through the project's real, unmodified loaders/validators.
    sources = list_semantic_sources(root)
    assert [s.name for s in sources] == ["widgets"]
    assert sources[0].meta.provenance == "human_curated"
    assert sources[0].meta.pack_source.pack == "widgets"
    assert sources[0].meta.pack_source.variant == "duckdb"

    bindings = load_metric_bindings(root)
    assert bindings[0].metric == "widget_count"
    assert bindings[0].pack_source.pack == "widgets"

    guardrails = load_guardrails(root)
    assert guardrails[0].filter == "region != 'eu'"
    assert guardrails[0].pack_source.pack == "widgets"

    validate_contracts(root)  # raises on any cross-surface break

    page = load_knowledge_page(root / "knowledge" / "global" / "widget-notes.md")
    assert page.sl_refs == ["widgets_db.widgets"]
    assert page.meta.pack_source.pack == "widgets"


def test_install_pack_is_idempotent_on_reinstall(tmp_path, fixture_pack_dir, widgets_duckdb):
    """A version-bump reinstall overwrites the same target files (§2.3), not duplicates."""
    root = tmp_path / "project"
    scaffold_project(root)
    manifest = load_pack_manifest(fixture_pack_dir)
    variant = manifest.variant("duckdb")
    params = resolve_params(manifest, {"connection_id": "widgets_db", "status_filter": "active"})
    connection = _widgets_connection(widgets_duckdb)
    check_required_tables(manifest, params, connection)

    install_pack(root, fixture_pack_dir, manifest, variant, params)
    install_pack(root, fixture_pack_dir, manifest, variant, params)

    sources = list_semantic_sources(root)
    assert len(sources) == 1


def test_install_pack_refuses_a_too_old_canonic_before_writing(tmp_path, fixture_pack_dir):
    """A pack that needs a newer canonic must not install a silently degraded copy."""
    root = tmp_path / "project"
    scaffold_project(root)
    manifest = load_pack_manifest(fixture_pack_dir).model_copy(
        update={"min_canonic_version": "999.0.0"}
    )
    variant = manifest.variant("duckdb")

    with pytest.raises(PackError, match="needs canonic 999.0.0 or newer"):
        install_pack(root, fixture_pack_dir, manifest, variant, {"connection_id": "widgets_db"})

    assert not (root / "semantics" / "widgets_db").exists()
