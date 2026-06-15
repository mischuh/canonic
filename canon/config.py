"""Project configuration model and loader for canon.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

KNOWN_VERSIONS: frozenset[int] = frozenset({1})
_REF_PATTERN = re.compile(r"^(env:|keyring:|file:)")

#: Committed context directories scaffolded for every project (SPEC E1 §2).
CONTEXT_DIRS: tuple[str, ...] = ("semantics", "knowledge", "contracts", "raw-sources")
#: Git-ignored local state/secret directory (SPEC E1 §7).
LOCAL_STATE_DIR = ".canon"


class ConfigError(Exception):
    """Raised when canon.yaml is invalid, missing, or uses an unknown version."""


class ProjectConfig(BaseModel):
    name: str
    default_connection: str | None = None


class Connection(BaseModel):
    id: str
    type: str
    params: dict[str, Any] = {}
    credentials_ref: str
    read_only_role: str | None = None

    @field_validator("credentials_ref")
    @classmethod
    def _reject_literal_secret(cls, v: str) -> str:
        if not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v


class LLMConfig(BaseModel):
    provider: str
    base_url: str
    model: str
    api_key_ref: str | None = None
    tasks: dict[str, str] = {}

    @field_validator("api_key_ref")
    @classmethod
    def _reject_literal_api_key(cls, v: str | None) -> str | None:
        if v is not None and not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v


class TelemetryConfig(BaseModel):
    enabled: bool = False


class YamlConfigSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that reads a canon.yaml file via ruamel.yaml."""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            yaml = YAML()
            with open(self._path) as f:
                raw = yaml.load(f)
            self._cache = dict(raw) if raw else {}
        return self._cache

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._load().get(field_name), field_name, self.field_is_complex(field)

    def field_is_complex(self, field: FieldInfo) -> bool:
        return True

    def __call__(self) -> dict[str, Any]:
        return self._load()


class CanonConfig(BaseSettings):
    """Validated project configuration loaded from canon.yaml."""

    model_config = SettingsConfigDict(extra="ignore")

    version: int
    project: ProjectConfig
    connections: list[Connection] = []
    llm: LLMConfig
    telemetry: TelemetryConfig = TelemetryConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        root = find_project_root()
        if root is not None:
            return (init_settings, YamlConfigSource(settings_cls, root / "canon.yaml"))
        return (init_settings,)


def find_project_root() -> Path | None:
    """Walk up from cwd looking for canon.yaml; return its directory or None."""
    current = Path.cwd()
    for directory in (current, *current.parents):
        if (directory / "canon.yaml").exists():
            return directory
    return None


def dump_config(config: CanonConfig, path: Path) -> None:
    """Write a validated CanonConfig to path as canon.yaml.

    Round-trips with load_config: the result re-parses to an equal config. Only
    indirection strings (``*_ref``) are written — secret values never touch disk.
    """
    data = config.model_dump(mode="json", exclude_none=True)
    llm = data.get("llm")
    if isinstance(llm, dict) and not llm.get("tasks"):
        llm.pop("tasks", None)
    yaml = YAML()
    yaml.default_flow_style = False
    with open(path, "w") as f:
        yaml.dump(data, f)


def scaffold_project(root: Path) -> list[Path]:
    """Create the canon project skeleton under root; return the paths created.

    Idempotent — existing files and directories are left untouched. Scaffolds the
    four committed context directories (SPEC E1 §2), the restrictive-permission
    ``.canon/`` local-state directory (§7), and a ``.gitignore`` covering
    ``.canon/`` when none exists yet.
    """
    from canon.contracts.loader import contracts_dir_scaffold

    created: list[Path] = []
    for name in CONTEXT_DIRS:
        directory = root / name
        if not directory.exists():
            created.append(directory)
        directory.mkdir(parents=True, exist_ok=True)
    contracts_dir_scaffold(root)

    local = root / LOCAL_STATE_DIR
    if not local.exists():
        created.append(local)
    local.mkdir(parents=True, exist_ok=True)
    local.chmod(0o700)

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(f"{LOCAL_STATE_DIR}/\n")
        created.append(gitignore)
    return created


def load_config(path: Path) -> CanonConfig:
    """Load and validate canon.yaml at path, raising ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    yaml = YAML()
    try:
        with open(path) as f:
            raw: dict[str, Any] = yaml.load(f) or {}
    except Exception as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    version = raw.get("version")
    if version not in KNOWN_VERSIONS:
        raise ConfigError(f"unknown config version {version}, upgrade canon")

    try:
        return CanonConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = " → ".join(str(p) for p in first["loc"])
        raise ConfigError(f"{loc}: {first['msg']}") from exc
