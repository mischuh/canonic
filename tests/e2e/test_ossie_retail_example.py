"""examples/ossie-retail stays reproducible from its Ossie model.

Starts from the example without any drafted context (no semantics/, contracts/, knowledge/)
and walks the README: bootstrap, ingest, apply. The result must equal the committed files,
and a further run over it must change nothing. If the connector or builder changes what an
Ossie model maps to, this fails and the example gets regenerated deliberately.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from canonic.cli.app import app

_EXAMPLE = Path(__file__).parents[2] / "examples" / "ossie-retail"
_DRAFTED = ("semantics", "contracts", "knowledge")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(
        _EXAMPLE,
        tmp_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".canonic", "retail.db", "raw-sources", *_DRAFTED),
    )
    with sqlite3.connect(tmp_path / "retail.db") as conn:
        conn.executescript((tmp_path / "setup.sql").read_text())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _invoke(*args: str) -> str:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.stdout


def _drafted_files(root: Path) -> dict[str, object]:
    """Every drafted file, with the run-specific freshness stamp removed."""
    yaml = YAML(typ="safe")
    files: dict[str, object] = {}
    for top in _DRAFTED:
        for path in sorted((root / top).rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(root).as_posix()
            if path.suffix == ".yaml":
                content = yaml.load(path.read_text())
                (content.get("meta") or {}).pop("last_validated_at", None)
                files[key] = content
            else:
                files[key] = path.read_text()
    return files


def test_readme_walkthrough_reproduces_the_committed_example(project: Path) -> None:
    _invoke("ingest", "--bootstrap", "--headless", "--no-pr")
    _invoke("ingest", "--headless", "--no-pr")
    run = sorted((project / ".canonic" / "pending-diffs").iterdir())[-1]
    _invoke("apply", str(run))

    assert _drafted_files(project) == _drafted_files(_EXAMPLE)


def test_rerun_over_the_committed_example_changes_nothing(project: Path) -> None:
    for top in _DRAFTED:
        shutil.copytree(_EXAMPLE / top, project / top)

    report = json.loads(_invoke("--json", "ingest", "--headless", "--no-pr", "--dry-run"))

    decisions = {e["decision"] for e in report["report"]["entries"]}
    assert decisions == {"no_op"}
    assert len(report["report"]["entries"]) == 16
