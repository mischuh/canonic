"""Shared fixtures for CLI tests."""

import pytest
from typer.testing import CliRunner

_VALID_CONFIG = """\
version: 1
project:
  name: test-project
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A temp directory that is a valid canon project (cwd switched into it)."""
    (tmp_path / "canon.yaml").write_text(_VALID_CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def outside_project(monkeypatch, tmp_path):
    """Run from a temp dir with no canon.yaml and no last-project fallback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("canon.cli.commands.mcp._load_last_project", lambda: None)
