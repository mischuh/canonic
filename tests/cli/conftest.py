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


@pytest.fixture(autouse=True)
def _no_live_pack_repo(monkeypatch, tmp_path):
    """Default CANONIC_PACKS_REPO to a local, nonexistent path for every CLI test.

    Without this, `canonic setup`'s context-packs branch and `canonic pack` fall back to
    the real canonic-packs GitHub repo (canonic/cli/commands/{setup,pack}.py), making
    tests depend on live network access and that repo's current, externally-mutable
    content — exactly what broke tests/cli/test_setup.py once the posthog pack merged to
    canonic-packs' main (a pack started being offered where none was before). Tests that
    specifically exercise the pack mechanism override this themselves (see
    tests/cli/test_setup_packs.py, tests/cli/test_pack.py's --repo flag).
    """
    monkeypatch.setenv("CANONIC_PACKS_REPO", str(tmp_path / "no-pack-repo-in-tests"))


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A temp directory that is a valid canonic project (cwd switched into it)."""
    (tmp_path / "canonic.yaml").write_text(_VALID_CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def outside_project(monkeypatch, tmp_path):
    """Run from a temp dir with no canonic.yaml and no last-project fallback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("canonic.cli.commands.mcp._load_last_project", lambda: None)
