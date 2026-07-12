"""Tests for ``canonic mcp`` CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

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

# CanonicService and daemon functions are lazy-imported inside start(), so
# patches must target their definition modules, not mcp itself.
_PATCH_SERVICE = "canonic.core.service.CanonicService"
_PATCH_START_HTTP = "canonic.mcp.daemon.start_http"
_PATCH_START_STDIO = "canonic.mcp.daemon.start_stdio"
_PATCH_BUILD_VERIFIER = "canonic.mcp.auth.build_token_verifier"


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "canonic.yaml").write_text(_VALID_CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _mock_service() -> MagicMock:
    svc = MagicMock()
    svc.list_metrics.return_value = ["m1"]
    return svc


# ---------------------------------------------------------------------------
# _resolve_root: explicit --project
# ---------------------------------------------------------------------------


def test_start_explicit_project_resolves(runner: CliRunner, tmp_path: Path) -> None:
    """--project <valid dir> starts without needing cwd to be the project."""
    (tmp_path / "canonic.yaml").write_text(_VALID_CONFIG)

    with (
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP),
        patch(_PATCH_BUILD_VERIFIER, return_value=MagicMock()),
        patch("canonic.cli.commands.mcp._save_last_project"),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(
            app, ["mcp", "start", "--transport", "http", "--project", str(tmp_path)]
        )

    assert result.exit_code == 0, result.output
    mock_cls.from_project.assert_called_once_with(tmp_path.resolve())


def test_start_explicit_project_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    """--project pointing at dir with no canonic.yaml exits with an error."""
    result = runner.invoke(app, ["mcp", "start", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "canonic.yaml" in result.output


def test_start_explicit_project_short_flag(runner: CliRunner, tmp_path: Path) -> None:
    """-p is an alias for --project."""
    (tmp_path / "canonic.yaml").write_text(_VALID_CONFIG)

    with (
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP),
        patch(_PATCH_BUILD_VERIFIER, return_value=MagicMock()),
        patch("canonic.cli.commands.mcp._save_last_project"),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(app, ["mcp", "start", "--transport", "http", "-p", str(tmp_path)])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# _resolve_root: last-project fallback
# ---------------------------------------------------------------------------


def test_last_project_fallback_used_when_no_cwd_match(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    """When cwd has no canonic.yaml but _load_last_project points at a valid project."""
    (tmp_path / "canonic.yaml").write_text(_VALID_CONFIG)

    nowhere = tmp_path / "not-a-project"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)

    with (
        patch("canonic.cli.commands.mcp._load_last_project", return_value=tmp_path),
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP),
        patch(_PATCH_BUILD_VERIFIER, return_value=MagicMock()),
        patch("canonic.cli.commands.mcp._save_last_project"),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(app, ["mcp", "start", "--transport", "http"])

    assert result.exit_code == 0, result.output
    mock_cls.from_project.assert_called_once_with(tmp_path)


def test_no_project_anywhere_exits_with_error(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    """No cwd match, no last-project → exit 1 with helpful message."""
    nowhere = tmp_path / "empty"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)

    with patch("canonic.cli.commands.mcp._load_last_project", return_value=None):
        result = runner.invoke(app, ["mcp", "start"])

    assert result.exit_code == 1
    assert "--project" in result.output


# ---------------------------------------------------------------------------
# _save_last_project is called after successful load
# ---------------------------------------------------------------------------


def test_start_saves_last_project(runner: CliRunner, project_dir: Path) -> None:
    """Successful start writes the project root to the last-project file."""
    saved: list[Path] = []

    with (
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP),
        patch(_PATCH_BUILD_VERIFIER, return_value=MagicMock()),
        patch("canonic.cli.commands.mcp._save_last_project", side_effect=saved.append),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(app, ["mcp", "start", "--transport", "http"])

    assert result.exit_code == 0, result.output
    assert saved == [project_dir]


# ---------------------------------------------------------------------------
# --transport http auth requirement (AMENDMENT-remote-mcp-transport.md)
# ---------------------------------------------------------------------------


def test_start_http_without_token_exits_error(runner: CliRunner, project_dir: Path) -> None:
    """--transport http with no mcp.auth.tokens and no --token-ref is a hard error."""
    with (
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP) as mock_start_http,
        patch("canonic.cli.commands.mcp._save_last_project"),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(app, ["mcp", "start", "--transport", "http"])

    assert result.exit_code == 1
    assert "bearer token" in result.output
    mock_start_http.assert_not_called()


def test_start_http_with_token_ref_succeeds(
    runner: CliRunner, project_dir: Path, monkeypatch
) -> None:
    """--token-ref resolves a real token via env: and lets --transport http proceed."""
    monkeypatch.setenv("CANONIC_TEST_MCP_TOKEN", "s3cr3t")

    with (
        patch(_PATCH_SERVICE) as mock_cls,
        patch(_PATCH_START_HTTP) as mock_start_http,
        patch("canonic.cli.commands.mcp._save_last_project"),
    ):
        mock_cls.from_project.return_value = _mock_service()
        result = runner.invoke(
            app,
            ["mcp", "start", "--transport", "http", "--token-ref", "env:CANONIC_TEST_MCP_TOKEN"],
        )

    assert result.exit_code == 0, result.output
    mock_start_http.assert_called_once()
    assert mock_start_http.call_args.kwargs["auth"] is not None
