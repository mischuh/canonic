"""Tests for canon.config — covers all acceptance criteria from GH-2."""

from pathlib import Path

import pytest

from canon.config import (
    CanonConfig,
    ConfigError,
    find_project_root,
    load_config,
)

_VALID = """\
version: 1
project:
  name: test-project
connections:
  - id: warehouse_pg
    type: postgres
    params:
      host: db.internal
      port: 5432
    credentials_ref: env:CANON_PG_DSN
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
  api_key_ref: env:CANON_LLM_KEY
telemetry:
  enabled: false
"""


def _canon_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "canon.yaml"
    p.write_text(content)
    return p


class TestLoadConfig:
    def test_valid_config_parses_fields(self, tmp_path: Path) -> None:
        cfg = load_config(_canon_yaml(tmp_path, _VALID))
        assert cfg.version == 1
        assert cfg.project.name == "test-project"
        assert cfg.project.default_connection is None
        assert len(cfg.connections) == 1
        conn = cfg.connections[0]
        assert conn.id == "warehouse_pg"
        assert conn.type == "postgres"
        assert conn.params == {"host": "db.internal", "port": 5432}
        assert conn.credentials_ref == "env:CANON_PG_DSN"
        assert cfg.llm.provider == "openai_compatible"
        assert cfg.llm.model == "llama3"
        assert cfg.llm.api_key_ref == "env:CANON_LLM_KEY"
        assert cfg.telemetry.enabled is False

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "canon.yaml")

    def test_unknown_version_raises_upgrade_message(self, tmp_path: Path) -> None:
        content = _VALID.replace("version: 1", "version: 999")
        with pytest.raises(ConfigError, match="unknown config version 999, upgrade canon"):
            load_config(_canon_yaml(tmp_path, content))

    def test_literal_secret_in_credentials_ref_raises(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "credentials_ref: env:CANON_PG_DSN", "credentials_ref: supersecret"
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canon_yaml(tmp_path, content))
        assert "credentials_ref" in str(exc_info.value)

    def test_literal_secret_in_api_key_ref_raises(self, tmp_path: Path) -> None:
        content = _VALID.replace("api_key_ref: env:CANON_LLM_KEY", "api_key_ref: sk-literalkey")
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canon_yaml(tmp_path, content))
        assert "api_key_ref" in str(exc_info.value)

    def test_keyring_ref_accepted(self, tmp_path: Path) -> None:
        content = _VALID.replace("env:CANON_PG_DSN", "keyring:pg-secret")
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.connections[0].credentials_ref == "keyring:pg-secret"

    def test_file_ref_accepted(self, tmp_path: Path) -> None:
        content = _VALID.replace("env:CANON_PG_DSN", "file:.canon/secrets/pg")
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.connections[0].credentials_ref == "file:.canon/secrets/pg"

    def test_nullable_api_key_ref(self, tmp_path: Path) -> None:
        content = _VALID.replace("  api_key_ref: env:CANON_LLM_KEY\n", "")
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.llm.api_key_ref is None

    def test_telemetry_defaults_to_disabled(self, tmp_path: Path) -> None:
        content = _VALID.replace("telemetry:\n  enabled: false\n", "")
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.telemetry.enabled is False

    def test_llm_tasks_per_task_override(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "  model: llama3", "  model: llama3\n  tasks:\n    reconcile: gpt-4"
        )
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.llm.tasks == {"reconcile": "gpt-4"}

    def test_empty_connections_allowed(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "connections:\n"
            "  - id: warehouse_pg\n"
            "    type: postgres\n"
            "    params:\n"
            "      host: db.internal\n"
            "      port: 5432\n"
            "    credentials_ref: env:CANON_PG_DSN\n",
            "connections: []\n",
        )
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.connections == []

    def test_extra_top_level_keys_are_ignored(self, tmp_path: Path) -> None:
        content = _VALID + "embeddings:\n  provider: local\n"
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.version == 1  # no error

    def test_returns_canon_config_instance(self, tmp_path: Path) -> None:
        cfg = load_config(_canon_yaml(tmp_path, _VALID))
        assert isinstance(cfg, CanonConfig)

    def test_default_connection_parsed(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "  name: test-project", "  name: test-project\n  default_connection: warehouse_pg"
        )
        cfg = load_config(_canon_yaml(tmp_path, content))
        assert cfg.project.default_connection == "warehouse_pg"


class TestFindProjectRoot:
    def test_returns_none_outside_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert find_project_root() is None

    def test_returns_root_directory_when_in_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canon.yaml").touch()
        monkeypatch.chdir(tmp_path)
        assert find_project_root() == tmp_path

    def test_finds_root_from_nested_subdirectory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canon.yaml").touch()
        nested = tmp_path / "src" / "connectors"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert find_project_root() == tmp_path

    def test_stops_at_first_canon_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canon.yaml").touch()
        inner = tmp_path / "subproject"
        inner.mkdir()
        (inner / "canon.yaml").touch()
        monkeypatch.chdir(inner)
        assert find_project_root() == inner
