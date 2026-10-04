"""``canonic pack add`` end-to-end (AMENDMENT-context-packs), via the fixture pack in
``tests/packs/conftest.py``.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from canonic.config import (
    CanonicConfig,
    Connection,
    ProjectConfig,
    TelemetryConfig,
    dump_config,
    scaffold_project,
)
from tests.packs.conftest import write_fixture_pack, write_unreadable_pack, write_variant_pack


def _seed_widgets_db(db_path) -> None:
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE widgets (id VARCHAR, status VARCHAR, region VARCHAR, created_at TIMESTAMP);"
        "INSERT INTO widgets VALUES "
        "('1', 'active', 'us', '2026-09-01 00:00:00'),"
        "('2', 'active', 'eu', '2026-09-02 00:00:00'),"
        "('3', 'inactive', 'apac', '2026-09-03 00:00:00');"
    )
    con.close()


@pytest.fixture
def pack_project(tmp_path, monkeypatch):
    """A scaffolded project with one duckdb connection, cwd switched into it."""
    root = tmp_path / "project"
    scaffold_project(root)
    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)
    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="pack-project", default_connection="widgets_db"),
        connections=[
            Connection(id="widgets_db", type="duckdb", params={"path": str(db_path)}),
        ],
        telemetry=TelemetryConfig(),
    )
    dump_config(config, root / "canonic.yaml")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def pack_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    return repo_dir


@pytest.fixture
def variant_pack_repo(tmp_path):
    repo_dir = tmp_path / "variant_repo"
    write_variant_pack(repo_dir)
    return repo_dir


def _params_file(tmp_path, **overrides):
    values = {"connection_id": "widgets_db", "status_filter": "active"}
    values.update(overrides)
    path = tmp_path / "params.json"
    path.write_text(json.dumps(values))
    return path


def test_pack_add_non_interactive_installs_and_runs_first_answer(
    runner, pack_project, pack_repo, tmp_path
):
    from canonic.cli.app import app

    params_file = _params_file(tmp_path, excluded_regions="eu")

    result = runner.invoke(
        app,
        ["pack", "add", "widgets", "--repo", str(pack_repo), "--params-file", str(params_file)],
    )

    assert result.exit_code == 0, result.output
    assert "installed widgets" in result.output
    assert (pack_project / "semantics" / "widgets_db" / "widgets.yaml").exists()
    assert (pack_project / "contracts" / "metrics" / "widget_count.yaml").exists()
    assert (pack_project / "contracts" / "guardrails" / "widgets-exclude-regions.yaml").exists()
    assert (pack_project / "knowledge" / "global" / "widget-notes.md").exists()
    assert "first answer" in result.output


def test_pack_add_missing_required_param_reported(runner, pack_project, pack_repo, tmp_path):
    """§5.2: ``connection_id`` is auto-bound (one matching connection exists), never asked
    as an ordinary param — only the genuinely-missing ``status_filter`` is reported."""
    from canonic.cli.app import app

    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps({}))

    result = runner.invoke(
        app,
        ["pack", "add", "widgets", "--repo", str(pack_repo), "--params-file", str(params_file)],
    )

    assert result.exit_code != 0
    assert "status_filter" in result.output
    assert "status_filter" in result.output


def test_pack_add_unknown_pack_errors(runner, pack_project, pack_repo, tmp_path):
    from canonic.cli.app import app

    params_file = _params_file(tmp_path)
    result = runner.invoke(
        app,
        ["pack", "add", "nonexistent", "--repo", str(pack_repo), "--params-file", str(params_file)],
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_pack_list_shows_fixture_pack(runner, pack_project, pack_repo):
    from canonic.cli.app import app

    result = runner.invoke(app, ["pack", "list", "--repo", str(pack_repo)])
    assert result.exit_code == 0, result.output
    assert "widgets" in result.output


def test_pack_list_skips_an_unreadable_pack_and_says_so(runner, pack_project, pack_repo):
    from canonic.cli.app import app

    write_unreadable_pack(pack_repo)

    result = runner.invoke(app, ["pack", "list", "--repo", str(pack_repo)])

    assert result.exit_code == 0, result.output
    assert "widgets" in result.output
    assert "skipped future" in result.output
    assert "Extra inputs are not permitted" in result.output


def test_pack_list_json_reports_the_skipped_packs(runner, pack_project, pack_repo):
    from canonic.cli.app import app

    write_unreadable_pack(pack_repo)

    result = runner.invoke(app, ["--json", "pack", "list", "--repo", str(pack_repo)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [p["pack"] for p in data["packs"]] == ["widgets"]
    assert [s["pack"] for s in data["skipped"]] == ["future"]


def test_pack_list_json_has_an_empty_skipped_list_when_all_packs_load(
    runner, pack_project, pack_repo
):
    from canonic.cli.app import app

    result = runner.invoke(app, ["--json", "pack", "list", "--repo", str(pack_repo)])

    assert json.loads(result.output)["skipped"] == []


def test_pack_list_when_no_pack_can_be_read(runner, pack_project, tmp_path):
    from canonic.cli.app import app

    repo = tmp_path / "only_future"
    write_unreadable_pack(repo)

    result = runner.invoke(app, ["pack", "list", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "no readable packs found" in result.output
    assert "skipped future" in result.output


def test_pack_validate_still_fails_on_an_unreadable_pack(runner, tmp_path, monkeypatch):
    from canonic.cli.app import app

    write_fixture_pack(tmp_path)
    write_unreadable_pack(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pack", "validate", str(tmp_path)])

    assert result.exit_code != 0
    assert "Extra inputs are not permitted" in result.output


def test_pack_list_shows_every_variant(runner, pack_project, variant_pack_repo):
    from canonic.cli.app import app

    result = runner.invoke(app, ["--json", "pack", "list", "--repo", str(variant_pack_repo)])

    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.output)["packs"]
    assert entry["variants"] == ["a", "b"]


@pytest.mark.parametrize(
    ("variant", "expect_extra"),
    [("a", False), ("b", True)],
)
def test_pack_add_installs_the_files_of_the_chosen_variant(
    runner, pack_project, variant_pack_repo, tmp_path, variant, expect_extra
):
    from canonic.cli.app import app

    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps({}))

    result = runner.invoke(
        app,
        [
            "pack",
            "add",
            "widgets_variants",
            "--repo",
            str(variant_pack_repo),
            "--variant",
            variant,
            "--params-file",
            str(params_file),
        ],
    )

    assert result.exit_code == 0, result.output
    semantics = pack_project / "semantics" / "widgets_db"
    assert (semantics / "widgets.yaml").exists()
    assert (semantics / "widgets_extra.yaml").exists() is expect_extra
    assert (pack_project / "contracts" / "metrics" / "widget_count.yaml").exists()
    assert ("models/b/widgets_extra.yaml" in result.output) is expect_extra


# --- pack validate: no project, no connection, no DB needed at all -----------


def test_pack_validate_single_pack_dir_needs_no_project(runner, tmp_path, monkeypatch):
    """Deliberately no pack_project/pack_repo fixture here — just a bare tmp_path with no
    canonic.yaml, proving `pack validate` never calls find_project_root()."""
    from canonic.cli.app import app

    pack_dir = write_fixture_pack(tmp_path)  # returns tmp_path/packs/widgets directly
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pack", "validate", str(pack_dir)])
    assert result.exit_code == 0, result.output
    assert "widgets / duckdb" in result.output


def test_pack_validate_repo_root_discovers_every_pack(runner, tmp_path, monkeypatch):
    from canonic.cli.app import app

    write_fixture_pack(tmp_path)  # writes tmp_path/packs/widgets — tmp_path is the repo root
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pack", "validate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "widgets" in result.output


def test_pack_validate_broken_pack_exits_nonzero(runner, tmp_path, monkeypatch):
    from canonic.cli.app import app

    pack_dir = tmp_path / "broken"
    pack_dir.mkdir()
    (pack_dir / "guardrails").mkdir()
    (pack_dir / "pack.yaml").write_text(
        "pack: broken\n"
        "version: 0.1.0\n"
        "variants:\n"
        "  - id: duckdb\n"
        "    label: Broken\n"
        "    mapping: mappings/duckdb.yaml\n"
        "params:\n"
        "  - name: connection_id\n"
        "    required: true\n"
        "provides:\n"
        "  contracts:\n"
        "    guardrails: [guardrails/bad.yaml]\n"
    )
    (pack_dir / "guardrails" / "bad.yaml").write_text(
        "id: broken-guardrail\n"
        "applies_to: { source: does_not_exist }\n"
        "kind: mandatory_filter\n"
        'filter: "TRUE"\n'
        "severity: warn\n"
        'rationale: "test"\n'
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pack", "validate", str(pack_dir)])
    assert result.exit_code == 1, result.output
    assert "does_not_exist" in result.output


def test_pack_validate_unknown_path_errors(runner, tmp_path, monkeypatch):
    from canonic.cli.app import app

    monkeypatch.chdir(tmp_path)
    empty_dir = tmp_path / "not_a_pack"
    empty_dir.mkdir()

    result = runner.invoke(app, ["pack", "validate", str(empty_dir)])
    assert result.exit_code != 0
    # Rich wraps the error at the terminal width, which varies by environment (narrower
    # in CI than a local dev terminal) — collapse whitespace so wrapping can't split the
    # phrase being asserted on across lines.
    assert "not a pack directory" in " ".join(result.output.split())


def test_pack_validate_reports_each_variant_with_its_own_file_count(
    runner, variant_pack_repo, monkeypatch
):
    from canonic.cli.app import app

    monkeypatch.chdir(variant_pack_repo)

    result = runner.invoke(app, ["--json", "pack", "validate", str(variant_pack_repo)])

    assert result.exit_code == 0, result.output
    reports = {r["variant"]: r for r in json.loads(result.output)["results"]}
    assert reports["a"]["files"] == 3
    assert reports["b"]["files"] == 4
    assert reports["a"]["ok"] and reports["b"]["ok"]


def test_pack_validate_fails_a_variant_without_failing_the_others(
    runner, variant_pack_repo, monkeypatch
):
    """A defect in a variant's own file fails that variant. The shared files and the other
    variant are validated on their own."""
    from canonic.cli.app import app

    pack_dir = variant_pack_repo / "packs" / "widgets_variants"
    extra = pack_dir / "models" / "b" / "widgets_extra.yaml"
    extra.write_text(extra.read_text().replace("{{table}}", "{{undeclared_param}}"))
    monkeypatch.chdir(variant_pack_repo)

    result = runner.invoke(app, ["pack", "validate", str(pack_dir), "--variant", "a"])
    assert result.exit_code == 0, result.output
    assert "widgets_variants / a" in result.output

    result = runner.invoke(app, ["pack", "validate", str(pack_dir), "--variant", "b"])
    assert result.exit_code != 0
    assert "undeclared_param" in result.output
