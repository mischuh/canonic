"""``canonic setup`` wizard integration for context packs (AMENDMENT-context-packs §5),
via the fixture pack in ``tests/packs/conftest.py``.
"""

from __future__ import annotations

import duckdb

from canonic.cli.app import app
from canonic.config import load_config
from canonic.exc import Unresolved
from canonic.instrumentation.models import FunnelMilestone
from canonic.instrumentation.report import read_events
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


def test_pack_branch_unreachable_repo_falls_through(runner, tmp_path, monkeypatch):
    """A network/repo failure never blocks setup — falls through to the unchanged path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(tmp_path / "does-not-exist"))
    skip_conn_input = "\n".join(
        [
            "",  # project name
            "n",  # configure a connection now? → No
            "",  # configure an llm now? → default yes
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
        ]
    )
    result = runner.invoke(app, ["setup"], input=skip_conn_input + "\n")
    assert result.exit_code == 0, result.output
    assert "pack repo unreachable" in result.output
    assert (tmp_path / "canonic.yaml").exists()


def test_pack_branch_something_else_falls_through(runner, tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))

    other_idx = "2"  # 1 = widgets pack, 2 = "something else"
    wizard_input = "\n".join(
        [
            "",  # project name
            other_idx,
            "n",  # configure a connection now? → No
            "",  # configure an llm now? → default yes
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
        ]
    )
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")
    assert result.exit_code == 0, result.output
    assert "context packs" in result.output
    config = load_config(tmp_path / "project" / "canonic.yaml")
    assert config.connections == []


def test_pack_branch_installs_widgets_pack(runner, tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))

    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)

    wizard_input = "\n".join(
        [
            "",  # project name
            "1",  # pack picker: widgets
            "widgets_db",  # duckdb connection id
            str(db_path),  # duckdb file path
            "",  # table param (default "widgets")
            "",  # excluded_regions param (default "")
            "1",  # status_filter choose_from selection
            "",  # confirm write (default yes)
        ]
    )
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")
    assert result.exit_code == 0, result.output
    assert "installed widgets" in result.output
    assert "first answer" in result.output
    assert "definition" in result.output
    assert "widget_count = count(*) on widgets" in result.output
    assert "widgets-exclude-regions" in result.output  # the guardrail the result carries
    milestones = [e.milestone for e in read_events(project_dir, kind="funnel_milestone")]
    assert FunnelMilestone.FIRST_ANSWER_SERVED in milestones

    config = load_config(project_dir / "canonic.yaml")
    assert [c.id for c in config.connections] == ["widgets_db"]
    assert (project_dir / "semantics" / "widgets_db" / "widgets.yaml").exists()
    assert (project_dir / "contracts" / "metrics" / "widget_count.yaml").exists()
    assert (project_dir / "knowledge" / "global" / "widget-notes.md").exists()


def test_pack_branch_exits_non_zero_when_the_first_answer_fails(runner, tmp_path, monkeypatch):
    """§5.6: files stay in place, the registry error is printed and setup does not say it is ready."""
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))
    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)

    def _fail(root, spec):
        raise Unresolved("first_answer metric 'widget_count' matches no active binding")

    monkeypatch.setattr("canonic.packs.first_answer.run_first_answer", _fail)

    wizard_input = "\n".join(["", "1", "widgets_db", str(db_path), "", "", "1", ""])
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")

    assert result.exit_code == 1, result.output
    assert "first answer failed" in result.output
    assert "setup incomplete" in result.output
    assert "is ready" not in result.output
    assert (project_dir / "contracts" / "metrics" / "widget_count.yaml").exists()
    milestones = [e.milestone for e in read_events(project_dir, kind="funnel_milestone")]
    assert FunnelMilestone.FIRST_ANSWER_SERVED not in milestones


def test_first_answer_says_when_internal_traffic_is_not_excluded(
    runner, tmp_path, monkeypatch, capsys
):
    from canonic.cli.commands._pack_prompts import run_and_render_first_answer
    from canonic.packs.manifest import FirstAnswer

    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))
    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)
    runner.invoke(
        app,
        ["setup"],
        input="\n".join(["", "1", "widgets_db", str(db_path), "", "", "1", ""]) + "\n",
    )
    spec = FirstAnswer(metric="widget_count")

    run_and_render_first_answer(project_dir, spec, {"internal_user_filter": "TRUE"})
    assert "Internal traffic is not excluded" in capsys.readouterr().out

    run_and_render_first_answer(
        project_dir, spec, {"internal_user_filter": "email NOT LIKE '%@acme.com'"}
    )
    assert "Internal traffic is not excluded" not in capsys.readouterr().out

    run_and_render_first_answer(project_dir, spec, {})
    assert "Internal traffic is not excluded" not in capsys.readouterr().out


def test_existing_project_menu_installs_pack(runner, tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    write_fixture_pack(repo_dir)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "canonic.yaml").write_text("version: 1\nproject:\n  name: existing-project\n")
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))

    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)

    menu_input = "\n".join(
        [
            "5",  # add a context pack
            "1",  # pack picker: widgets
            "widgets_db",  # duckdb connection id
            str(db_path),  # duckdb file path
            "",  # table param (default "widgets")
            "",  # excluded_regions param (default "")
            "1",  # status_filter choose_from selection
            "",  # confirm write (default yes)
            "6",  # exit
        ]
    )
    result = runner.invoke(app, ["setup"], input=menu_input + "\n")
    assert result.exit_code == 0, result.output
    assert "installed widgets" in result.output

    config = load_config(project_dir / "canonic.yaml")
    assert [c.id for c in config.connections] == ["widgets_db"]
    assert (project_dir / "semantics" / "widgets_db" / "widgets.yaml").exists()


def test_pack_branch_previews_and_installs_the_chosen_variants_files(runner, tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    write_variant_pack(repo_dir)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))

    db_path = tmp_path / "widgets.duckdb"
    _seed_widgets_db(db_path)

    wizard_input = "\n".join(
        [
            "",  # project name
            "1",  # pack picker: widgets_variants
            "2",  # variant picker: b
            "widgets_db",  # duckdb connection id
            str(db_path),  # duckdb file path
            "",  # table param (variant b's own, default "widgets")
            "",  # note param (only variant b declares it)
            "",  # confirm write (default yes)
        ]
    )
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")

    assert result.exit_code == 0, result.output
    assert "models/b/widgets_extra.yaml" in result.output
    assert "models/a/widgets.yaml" not in result.output
    assert "installed widgets_variants" in result.output
    semantics = project_dir / "semantics" / "widgets_db"
    assert (semantics / "widgets.yaml").exists()
    assert (semantics / "widgets_extra.yaml").exists()


def test_pack_branch_offers_the_readable_packs_and_names_the_skipped_one(
    runner, tmp_path, monkeypatch
):
    repo_dir = tmp_path / "repo"
    write_unreadable_pack(repo_dir)
    write_fixture_pack(repo_dir)
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(repo_dir))

    other_idx = "2"  # 1 = widgets pack, 2 = "something else"
    wizard_input = "\n".join(
        [
            "",  # project name
            other_idx,
            "n",  # configure a connection now? -> No
            "",  # configure an llm now? -> default yes
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
        ]
    )
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")

    assert result.exit_code == 0, result.output
    assert "skipping pack future" in result.output
    assert "widgets" in result.output
    assert "pack repo unreachable" not in result.output
