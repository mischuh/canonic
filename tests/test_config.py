"""Tests for canonic.config — covers all acceptance criteria from GH-2."""

from pathlib import Path

import pytest

from canonic.config import (
    CanonicConfig,
    ConfigError,
    find_project_root,
    load_config,
)
from canonic.exc import AirGappedViolation

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
    credentials_ref: env:CANONIC_PG_DSN
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
  api_key_ref: env:CANONIC_LLM_KEY
telemetry:
  enabled: false
"""


def _canonic_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "canonic.yaml"
    p.write_text(content)
    return p


class TestLoadConfig:
    def test_valid_config_parses_fields(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _VALID))
        assert cfg.version == 1
        assert cfg.project.name == "test-project"
        assert cfg.project.default_connection is None
        assert len(cfg.connections) == 1
        conn = cfg.connections[0]
        assert conn.id == "warehouse_pg"
        assert conn.type == "postgres"
        assert conn.params == {"host": "db.internal", "port": 5432}
        assert conn.credentials_ref == "env:CANONIC_PG_DSN"
        assert cfg.llm.provider == "openai_compatible"
        assert cfg.llm.model == "llama3"
        assert cfg.llm.api_key_ref == "env:CANONIC_LLM_KEY"
        assert cfg.telemetry.enabled is False

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "canonic.yaml")

    def test_unknown_version_raises_upgrade_message(self, tmp_path: Path) -> None:
        content = _VALID.replace("version: 1", "version: 999")
        with pytest.raises(ConfigError, match="unknown config version 999, upgrade canonic"):
            load_config(_canonic_yaml(tmp_path, content))

    def test_literal_secret_in_credentials_ref_raises(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "credentials_ref: env:CANONIC_PG_DSN", "credentials_ref: supersecret"
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "credentials_ref" in str(exc_info.value)

    def test_literal_secret_in_api_key_ref_raises(self, tmp_path: Path) -> None:
        content = _VALID.replace("api_key_ref: env:CANONIC_LLM_KEY", "api_key_ref: sk-literalkey")
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "api_key_ref" in str(exc_info.value)

    def test_keyring_ref_accepted(self, tmp_path: Path) -> None:
        content = _VALID.replace("env:CANONIC_PG_DSN", "keyring:pg-secret")
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.connections[0].credentials_ref == "keyring:pg-secret"

    def test_file_ref_accepted(self, tmp_path: Path) -> None:
        content = _VALID.replace("env:CANONIC_PG_DSN", "file:.canonic/secrets/pg")
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.connections[0].credentials_ref == "file:.canonic/secrets/pg"

    def test_nullable_api_key_ref(self, tmp_path: Path) -> None:
        content = _VALID.replace("  api_key_ref: env:CANONIC_LLM_KEY\n", "")
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm.api_key_ref is None

    def test_telemetry_defaults_to_disabled(self, tmp_path: Path) -> None:
        content = _VALID.replace("telemetry:\n  enabled: false\n", "")
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.telemetry.enabled is False

    def test_embeddings_defaults_when_block_absent(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _VALID))
        assert cfg.embeddings.model == "all-MiniLM-L6-v2"

    def test_embeddings_model_override(self, tmp_path: Path) -> None:
        content = _VALID + "embeddings:\n  model: bge-small-en\n"
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.embeddings.model == "bge-small-en"

    def test_embeddings_model_round_trips(self, tmp_path: Path) -> None:
        from canonic.config import dump_config

        content = _VALID + "embeddings:\n  model: bge-small-en\n"
        cfg = load_config(_canonic_yaml(tmp_path, content))
        out = tmp_path / "out.yaml"
        dump_config(cfg, out)
        assert load_config(out).embeddings.model == "bge-small-en"

    def test_llm_tasks_per_task_override(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "  model: llama3", "  model: llama3\n  tasks:\n    reconcile: gpt-4"
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm.tasks == {"reconcile": "gpt-4"}

    def test_empty_connections_allowed(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "connections:\n"
            "  - id: warehouse_pg\n"
            "    type: postgres\n"
            "    params:\n"
            "      host: db.internal\n"
            "      port: 5432\n"
            "    credentials_ref: env:CANONIC_PG_DSN\n",
            "connections: []\n",
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.connections == []

    def test_extra_top_level_keys_are_ignored(self, tmp_path: Path) -> None:
        content = _VALID + "embeddings:\n  provider: local\n"
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.version == 1  # no error

    def test_returns_canonic_config_instance(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _VALID))
        assert isinstance(cfg, CanonicConfig)

    def test_default_connection_parsed(self, tmp_path: Path) -> None:
        content = _VALID.replace(
            "  name: test-project", "  name: test-project\n  default_connection: warehouse_pg"
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.project.default_connection == "warehouse_pg"


class TestMcpAuthConfig:
    """``mcp.auth`` block (AMENDMENT-remote-mcp-transport.md)."""

    def test_defaults_when_block_absent(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _VALID))
        assert cfg.mcp.auth.tokens == []

    def test_tokens_parsed(self, tmp_path: Path) -> None:
        content = (
            _VALID
            + "mcp:\n"
            + "  auth:\n"
            + "    tokens:\n"
            + "      - client_id: alice\n"
            + "        token_ref: env:CANONIC_MCP_TOKEN_ALICE\n"
            + "      - client_id: bob\n"
            + "        token_ref: keyring:mcp-bob\n"
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert [(t.client_id, t.token_ref) for t in cfg.mcp.auth.tokens] == [
            ("alice", "env:CANONIC_MCP_TOKEN_ALICE"),
            ("bob", "keyring:mcp-bob"),
        ]

    def test_literal_token_ref_raises(self, tmp_path: Path) -> None:
        content = (
            _VALID
            + "mcp:\n"
            + "  auth:\n"
            + "    tokens:\n"
            + "      - client_id: alice\n"
            + "        token_ref: not-a-reference\n"
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "token_ref" in str(exc_info.value)


class TestLLMProviders:
    """Multi-provider ``llm.provider`` validation (SPEC-E10 §2)."""

    def test_unknown_provider_rejected(self, tmp_path: Path) -> None:
        content = _VALID.replace("provider: openai_compatible", "provider: made-up")
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "unknown llm.provider" in str(exc_info.value)

    def test_openai_compatible_without_base_url_rejected(self, tmp_path: Path) -> None:
        content = _VALID.replace("  base_url: http://localhost:11434/v1\n", "")
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "llm.base_url is required" in str(exc_info.value)

    def test_anthropic_provider_with_key_accepted(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace("provider: openai_compatible", "provider: anthropic")
            .replace("  base_url: http://localhost:11434/v1\n", "")
            .replace("model: llama3", "model: claude-opus-4-8")
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.base_url is None

    def test_anthropic_provider_without_key_rejected(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace("provider: openai_compatible", "provider: anthropic")
            .replace("  base_url: http://localhost:11434/v1\n", "")
            .replace("  api_key_ref: env:CANONIC_LLM_KEY\n", "")
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "llm.api_key_ref is required" in str(exc_info.value)

    def test_openai_provider_without_key_rejected(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace("provider: openai_compatible", "provider: openai")
            .replace("  base_url: http://localhost:11434/v1\n", "")
            .replace("  api_key_ref: env:CANONIC_LLM_KEY\n", "")
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "llm.api_key_ref is required" in str(exc_info.value)

    def test_github_copilot_without_key_accepted(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace("provider: openai_compatible", "provider: github_copilot")
            .replace("  base_url: http://localhost:11434/v1\n", "")
            .replace("  api_key_ref: env:CANONIC_LLM_KEY\n", "")
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm.provider == "github_copilot"
        assert cfg.llm.api_key_ref is None

    def test_github_copilot_with_key_rejected(self, tmp_path: Path) -> None:
        content = _VALID.replace("provider: openai_compatible", "provider: github_copilot").replace(
            "  base_url: http://localhost:11434/v1\n", ""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(_canonic_yaml(tmp_path, content))
        assert "llm.api_key_ref is not used" in str(exc_info.value)

    def test_air_gapped_blocks_hosted_provider_default_endpoint(self, tmp_path: Path) -> None:
        # No base_url configured for a hosted provider — the air-gapped check must still
        # fall back to its known public host rather than skip the check entirely.
        content = (
            _VALID.replace("provider: openai_compatible", "provider: anthropic").replace(
                "  base_url: http://localhost:11434/v1\n", ""
            )
            + "runtime:\n  air_gapped: true\n"
        )
        with pytest.raises(AirGappedViolation) as exc:
            load_config(_canonic_yaml(tmp_path, content))
        assert "llm.base_url" in str(exc.value)


class TestAirGapped:
    """Load-time air-gapped enforcement (SPEC-E10 §4, GH-63, S3/AC1+AC3)."""

    def test_air_gapped_with_local_endpoint_loads(self, tmp_path: Path) -> None:
        content = _VALID + "runtime:\n  air_gapped: true\n"
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.runtime.air_gapped is True

    def test_air_gapped_defaults_to_false(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _VALID))
        assert cfg.runtime.air_gapped is False
        assert cfg.runtime.allow_cidrs == []

    def test_air_gapped_with_public_base_url_refuses_to_start(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace(
                "base_url: http://localhost:11434/v1", "base_url: https://api.openai.com/v1"
            )
            + "runtime:\n  air_gapped: true\n"
        )
        with pytest.raises(AirGappedViolation) as exc:
            load_config(_canonic_yaml(tmp_path, content))
        assert exc.value.exit_code == 18
        assert "llm.base_url" in str(exc.value)

    def test_air_gapped_forces_telemetry_off(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace("telemetry:\n  enabled: false", "telemetry:\n  enabled: true")
            + "runtime:\n  air_gapped: true\n"
        )
        with pytest.raises(AirGappedViolation, match="telemetry.enabled must be false"):
            load_config(_canonic_yaml(tmp_path, content))

    def test_air_gapped_allows_lan_host_via_allow_cidrs(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace(
                "base_url: http://localhost:11434/v1", "base_url: http://10.1.2.3:11434/v1"
            )
            + "runtime:\n  air_gapped: true\n  allow_cidrs:\n    - 10.0.0.0/8\n"
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.runtime.allow_cidrs == ["10.0.0.0/8"]

    def test_air_gapped_rejects_unlisted_lan_host(self, tmp_path: Path) -> None:
        content = (
            _VALID.replace(
                "base_url: http://localhost:11434/v1", "base_url: http://10.1.2.3:11434/v1"
            )
            + "runtime:\n  air_gapped: true\n"
        )
        with pytest.raises(AirGappedViolation, match="not local or allowlisted"):
            load_config(_canonic_yaml(tmp_path, content))

    def test_not_air_gapped_allows_public_base_url(self, tmp_path: Path) -> None:
        # Regression guard: without air_gapped, a hosted endpoint is perfectly valid.
        content = _VALID.replace(
            "base_url: http://localhost:11434/v1", "base_url: https://api.openai.com/v1"
        )
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm.base_url == "https://api.openai.com/v1"


class TestFindProjectRoot:
    def test_returns_none_outside_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert find_project_root() is None

    def test_returns_root_directory_when_in_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canonic.yaml").touch()
        monkeypatch.chdir(tmp_path)
        assert find_project_root() == tmp_path

    def test_finds_root_from_nested_subdirectory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canonic.yaml").touch()
        nested = tmp_path / "src" / "connectors"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert find_project_root() == tmp_path

    def test_stops_at_first_canonic_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "canonic.yaml").touch()
        inner = tmp_path / "subproject"
        inner.mkdir()
        (inner / "canonic.yaml").touch()
        monkeypatch.chdir(inner)
        assert find_project_root() == inner


_NO_LLM = """\
version: 1
project:
  name: no-models-project
connections:
  - id: warehouse_pg
    type: postgres
    credentials_ref: env:CANONIC_PG_DSN
"""


class TestNoModelsOperatingPoint:
    """No-models-configured is a valid, fully-deterministic operating point (GH-68 S7)."""

    def test_config_without_llm_block_loads(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _NO_LLM))
        assert cfg.llm is None

    def test_no_llm_with_air_gapped_loads(self, tmp_path: Path) -> None:
        content = _NO_LLM + "runtime:\n  air_gapped: true\n"
        cfg = load_config(_canonic_yaml(tmp_path, content))
        assert cfg.llm is None
        assert cfg.runtime.air_gapped is True

    def test_no_llm_embeddings_defaults_apply(self, tmp_path: Path) -> None:
        cfg = load_config(_canonic_yaml(tmp_path, _NO_LLM))
        assert cfg.embeddings.model == "all-MiniLM-L6-v2"
