"""Project configuration model and loader for canonic.yaml."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from ruamel.yaml import YAML

from canonic.airgap import EgressPolicy, guard_telemetry
from canonic.llm_providers import PROVIDERS, CredentialMode
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

KNOWN_VERSIONS: frozenset[int] = frozenset({1})
_REF_PATTERN = re.compile(r"^(env:|keyring:|file:)")

#: Committed context directories scaffolded for every project (SPEC E1 §2).
CONTEXT_DIRS: tuple[str, ...] = ("semantics", "knowledge", "contracts", "raw-sources")
#: Git-ignored local state/secret directory (SPEC E1 §7).
LOCAL_STATE_DIR = ".canonic"


class ConfigError(Exception):
    """Raised when canonic.yaml is invalid, missing, or uses an unknown version."""


class ProjectConfig(BaseModel):
    name: str
    default_connection: str | None = None


class Connection(BaseModel):
    """A configured data source (SPEC E1 §3).

    ``params`` is a connector-specific bag (host, port, dbname, row_limit, ...). Postgres
    and Redshift additionally recognize ``schema`` (legacy single search_path string),
    ``schemas`` (list[str], preferred — also comma-joined into the search_path) and
    ``tables`` (list[str] of fully-qualified "schema.table" names or glob patterns) to
    narrow what ``introspect_schema()`` returns, plus ``fetch_column_stats`` (bool,
    default False) to additionally merge zero-scan cardinality/null-ratio column stats
    from ``pg_stats`` into the returned schema (used to improve LLM grain inference).
    Requesting ``fetch_column_stats`` on SQLite/DuckDB connections is a no-op (logged as
    a warning) — neither engine exposes queryable planner statistics without a data scan.
    A ``dbt`` connection additionally recognizes ``manifest_path`` (path to the compiled
    ``manifest.json``, default ``manifest.json``) and ``target_connection`` (the id of the
    physical, queryable connection whose tables this manifest describes — its
    ``RelationSchema`` evidence is stamped with that id, not this connection's own, so it
    reconciles against that connection's live-introspected sources instead of proposing
    independent ones; defaults to this connection's own id when omitted, matching a
    standalone dbt-only project with no paired physical connection). Validated against the
    configured connection ids by :meth:`CanonicConfig._validate_target_connections`.
    """

    id: str
    type: str
    params: dict[str, Any] = {}
    credentials_ref: str | None = None
    read_only_role: str | None = None

    @field_validator("credentials_ref")
    @classmethod
    def _reject_literal_secret(cls, v: str | None) -> str | None:
        # File-based connectors (e.g. dbt manifests) carry no secret; omitting the
        # ref is allowed. When present it must still be a reference, never a literal.
        if v is not None and not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v


class LLMConfig(BaseModel):
    """An ``llm`` block from ``canonic.yaml`` (SPEC-E1 §3, SPEC-E10 §2).

    ``provider`` selects how :class:`~canonic.runtime.generation.GenerationRuntime` routes
    the call through litellm (see :data:`canonic.llm_providers.PROVIDERS`).
    ``openai_compatible`` is the local/self-hosted path and always requires ``base_url``;
    hosted providers (``openai``, ``anthropic``, ``github_copilot``) reach litellm's own
    default endpoint unless ``base_url`` overrides it, and each has its own credential
    requirement — a bearer-token key, or none at all for a provider whose client manages
    its own auth (see :class:`~canonic.llm_providers.CredentialMode`).
    """

    provider: str
    base_url: str | None = None
    model: str
    api_key_ref: str | None = None
    tasks: dict[str, str] = {}

    @field_validator("api_key_ref")
    @classmethod
    def _reject_literal_api_key(cls, v: str | None) -> str | None:
        if v is not None and not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v

    @model_validator(mode="after")
    def _validate_provider(self) -> LLMConfig:
        spec = PROVIDERS.get(self.provider)
        if spec is None:
            raise ValueError(
                f"unknown llm.provider {self.provider!r}: must be one of "
                f"{', '.join(sorted(PROVIDERS))}"
            )
        if spec.requires_base_url and not self.base_url:
            raise ValueError(f"llm.base_url is required for provider {self.provider!r}")
        if spec.credential_mode is CredentialMode.REQUIRED and not self.api_key_ref:
            raise ValueError(f"llm.api_key_ref is required for provider {self.provider!r}")
        if spec.credential_mode is CredentialMode.FORBIDDEN and self.api_key_ref is not None:
            raise ValueError(
                f"llm.api_key_ref is not used for provider {self.provider!r} — remove it "
                "(this provider's client authenticates itself, outside canonic.yaml)"
            )
        return self

    @property
    def egress_check_url(self) -> str:
        """URL checked for air-gapped egress (SPEC-E10 §4).

        ``base_url`` when configured; otherwise the provider's known public endpoint —
        hosted providers always call a fixed host, so there is still something to
        allowlist-check even without an explicit override.
        """
        if self.base_url:
            return self.base_url
        host = PROVIDERS[self.provider].default_host
        assert host is not None  # every provider without a required base_url has one
        return f"https://{host}"


class EmbeddingConfig(BaseModel):
    """Local embedding runtime block from canonic.yaml (SPEC-E10 §5).

    Powers E6's optional vector-search arm. The backend is local
    ``sentence-transformers`` (an optional ``canonic[embeddings]`` add-on); when it is not
    installed E6 degrades to lexical-only. ``model`` names the sentence-transformers model
    to load; it is part of the identity fingerprint E6 uses to detect a model change and
    trigger a reindex, so changing it forces a clean rebuild rather than mixing vectors.
    """

    model: str = "all-MiniLM-L6-v2"


class TelemetryConfig(BaseModel):
    """Opt-in aggregate telemetry (SPEC-E16 §8/§12).

    ``enabled`` alone does not cause anything to be sent: a real send additionally
    requires ``endpoint`` and ``transport_acknowledged`` to be set (checked by
    :func:`canonic.airgap.guard_telemetry_send`), and is forced off entirely under
    ``runtime.air_gapped`` regardless of these fields.
    """

    enabled: bool = False
    endpoint: str | None = None
    #: Human attestation that this project has reviewed the exact aggregate payload
    #: (see ``canonic report --telemetry-preview``) before allowing a real send.
    #: canonic cannot verify that a review actually happened — this is a project-level
    #: policy decision recorded in canonic.yaml, not a technical guarantee.
    transport_acknowledged: bool = False
    auth_token_ref: str | None = None

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint_scheme(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("telemetry.endpoint must start with http:// or https://")
        return v

    @field_validator("auth_token_ref")
    @classmethod
    def _reject_literal_auth_token(cls, v: str | None) -> str | None:
        if v is not None and not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v


class RuntimeConfig(BaseModel):
    """Runtime behavior block from canonic.yaml (SPEC-E10 §4).

    ``air_gapped`` turns on the enforced privacy guarantee: every model endpoint must be
    local/allowlisted and no context may leave the machine. ``allow_cidrs`` opts in
    explicit private/LAN ranges for a separate on-prem inference host; it is only
    consulted when ``air_gapped`` is set (default: localhost-only).
    """

    air_gapped: bool = False
    allow_cidrs: list[str] = []


class AutoApplyConfig(BaseModel):
    """Governs whether a reconciliation proposal may be applied without review (SPEC-E4 §5.5).

    Auto-apply is opt-in, bounded by confidence, capped at the lowest provenance, and
    forbidden for structurally risky fields. The defaults keep ingest propose-only.
    """

    enabled: bool = False  # default: propose-only
    min_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    max_provenance: Provenance = Provenance.INFERRED  # never auto-apply over human_curated+
    never: list[str] = ["grain", "joins", "measures"]  # structural fields always need review


class ReconcileConfig(BaseModel):
    """Reconciliation policy block from canonic.yaml (SPEC-E4 §5.4, §5.5)."""

    auto_apply: AutoApplyConfig = AutoApplyConfig()
    # Strict CI mode that gates a run on contradictions; non-default (§5.4). Enforcement
    # (exit code) is the caller's; the engine never fails a run on contradictions itself.
    strict_contradictions: bool = False


class FeedbackConfig(BaseModel):
    """Feedback-loop policy from canonic.yaml (SPEC-E11 §4, §5, §8).

    Governs the pattern gate that turns recurring ``wrong_definition`` outcomes on a binding
    into contradiction evidence for E4 (§4) and how long a confirmed-wrong outcome caps that
    binding's served trust tier at ``caution`` (§5). Both are explicitly tunable — the spec
    (§9) leaves the right threshold as an open calibration question.
    """

    pattern_min_count: int = Field(default=2, ge=1)
    pattern_window_days: int = Field(default=90, ge=1)
    pattern_min_markers: int = Field(default=1, ge=1)
    trust_cap_window_days: int = Field(default=90, ge=1)


class LoggingConfig(BaseModel):
    """Logging policy from the ``logging:`` section of canonic.yaml."""

    level: str = "WARNING"
    file: str | None = None
    # "json" emits one JSON object per line — safe for stdio-transport MCP servers,
    # where stdout carries the JSON-RPC stream and logs must stay on stderr/file
    # but still be machine-parseable by whatever tails them.
    format: Literal["text", "json"] = "text"


class McpTokenEntry(BaseModel):
    """One bearer-token/client mapping for the MCP daemon's ``http`` transport."""

    client_id: str
    token_ref: str

    @field_validator("token_ref")
    @classmethod
    def _reject_literal_token(cls, v: str) -> str:
        if not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v


class McpOAuthMode(StrEnum):
    """Which OAuth 2.1 mechanism ``mcp.auth.oauth`` configures (AMENDMENT-oauth-mcp-auth)."""

    PROXY = "proxy"
    JWT = "jwt"


class McpOAuthConfig(BaseModel):
    """OAuth 2.1 auth for the MCP daemon's ``http`` transport (AMENDMENT-oauth-mcp-auth).

    Two modes, delegated to ``fastmcp``:

    - ``proxy``: ``OIDCProxy`` presents a DCR-compliant OAuth server to MCP clients and
      relays the actual login to a fixed, pre-registered upstream client at the IdP
      (Authorization Code + PKCE). Requires ``client_id`` and ``base_url`` (the daemon's
      own public URL); ``client_secret_ref`` is typically required too, unless the IdP
      allows a public client. See ``verify_id_token`` below for IdPs with opaque access
      tokens.
    - ``jwt``: the IdP hands the MCP client a JWT directly and the daemon only verifies
      its signature against the IdP's published JWKS. No proxy state, no redirect
      handling. Drops ``client_secret_ref`` and ``base_url`` entirely — there is no
      proxy to run.
    """

    mode: McpOAuthMode
    issuer_url: str
    client_id: str | None = None
    client_secret_ref: str | None = None
    scopes: list[str] = []
    base_url: str | None = None
    #: ``jwt`` mode only. Expected token audience; without it, any token the IdP issued
    #: for *any* resource is accepted, not just this daemon.
    audience: str | None = None
    #: ``jwt`` mode only. Skips OIDC discovery for IdPs that don't publish
    #: ``/.well-known/openid-configuration``.
    jwks_uri: str | None = None
    #: ``proxy`` mode only. FastMCP's ``OIDCProxy`` verifies the upstream *access* token
    #: by default, which many IdPs (Google, GitHub, some Okta setups) issue as an opaque,
    #: non-JWT string — verification then fails outright, not just poorly. Set to
    #: ``true`` to verify the OIDC *id_token* instead, which is always a standard JWT and
    #: reliably carries identity claims (``sub``/``email``); needed both for the proxy to
    #: work at all against such IdPs and for `AccessToken.client_id` (used in MCP request
    #: logging) to be a meaningful identity rather than an opaque subject id.
    verify_id_token: bool = False

    @field_validator("issuer_url")
    @classmethod
    def _validate_issuer_url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("mcp.auth.oauth.issuer_url must start with http:// or https://")
        return v

    @field_validator("client_secret_ref")
    @classmethod
    def _reject_literal_client_secret(cls, v: str | None) -> str | None:
        if v is not None and not _REF_PATTERN.match(v):
            raise ValueError("must be a reference (env:…, keyring:…, file:…), not a literal secret")
        return v

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> McpOAuthConfig:
        if self.mode == McpOAuthMode.PROXY:
            if self.client_id is None:
                raise ValueError("mcp.auth.oauth.client_id is required in proxy mode")
            if self.base_url is None:
                raise ValueError("mcp.auth.oauth.base_url is required in proxy mode")
        else:  # jwt
            if self.client_secret_ref is not None:
                raise ValueError("mcp.auth.oauth.client_secret_ref is not used in jwt mode")
            if self.base_url is not None:
                raise ValueError("mcp.auth.oauth.base_url is not used in jwt mode")
            if self.verify_id_token:
                raise ValueError("mcp.auth.oauth.verify_id_token is not used in jwt mode")
        return self


class McpAuthConfig(BaseModel):
    """Auth for the MCP daemon's ``http`` transport (AMENDMENT-remote-mcp-transport,
    AMENDMENT-oauth-mcp-auth).

    ``stdio`` transport needs none of this — process-level trust is sufficient for a
    local subprocess. ``http`` transport is network-reachable, so ``canonic mcp start
    --transport http`` refuses to start unless at least one token resolves here (or via
    the ``--token-ref`` CLI override) or ``oauth`` is configured. ``tokens`` and
    ``oauth`` are independently optional and compose: both configured is a supported
    state, not just tolerated (see ``canonic.mcp.auth.CanonicCompositeVerifier``).
    """

    tokens: list[McpTokenEntry] = []
    oauth: McpOAuthConfig | None = None


class McpConfig(BaseModel):
    """The ``mcp:`` block from canonic.yaml."""

    auth: McpAuthConfig = McpAuthConfig()


class YamlConfigSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that reads a canonic.yaml file via ruamel.yaml."""

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


class CanonicConfig(BaseSettings):
    """Validated project configuration loaded from canonic.yaml."""

    model_config = SettingsConfigDict(extra="ignore")

    version: int
    project: ProjectConfig
    connections: list[Connection] = []
    llm: LLMConfig | None = None
    embeddings: EmbeddingConfig = EmbeddingConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    reconcile: ReconcileConfig = ReconcileConfig()
    feedback: FeedbackConfig = FeedbackConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    logging: LoggingConfig = LoggingConfig()
    mcp: McpConfig = McpConfig()

    @model_validator(mode="after")
    def _enforce_air_gapped(self) -> CanonicConfig:
        """Load-time air-gapped enforcement (SPEC-E10 §4, S3/AC1+AC3).

        When ``runtime.air_gapped`` is set, refuse to load a config that could leak
        context off-machine: a public model endpoint, enabled telemetry, or a remote
        secret-service ``*_ref``. The daemon never starts mis-configured.
        """
        if not self.runtime.air_gapped:
            return self
        policy = EgressPolicy(allow_cidrs=self.runtime.allow_cidrs)
        # NOTE: ``embeddings`` is local-provider-only (sentence-transformers, no egress), so
        # there is no endpoint to validate under air-gapped. A future hosted embeddings
        # provider (#67) would validate its ``base_url`` here, mirroring ``llm.base_url``.
        if self.llm is not None:
            policy.check_url(self.llm.egress_check_url, what="llm.base_url")
            if self.llm.api_key_ref is not None:
                policy.check_ref_local(self.llm.api_key_ref, what="llm.api_key_ref")
        guard_telemetry(
            air_gapped=self.runtime.air_gapped, telemetry_enabled=self.telemetry.enabled
        )
        for conn in self.connections:
            if conn.credentials_ref is not None:
                policy.check_ref_local(
                    conn.credentials_ref, what=f"connections[{conn.id}].credentials_ref"
                )
        oauth = self.mcp.auth.oauth
        if oauth is not None:
            policy.check_url(oauth.issuer_url, what="mcp.auth.oauth.issuer_url")
            if oauth.client_secret_ref is not None:
                policy.check_ref_local(
                    oauth.client_secret_ref, what="mcp.auth.oauth.client_secret_ref"
                )
        return self

    @model_validator(mode="after")
    def _validate_target_connections(self) -> CanonicConfig:
        """A connection's ``params.target_connection`` must name a configured connection.

        Catches a typo'd or dangling reference at load time (fail fast) rather than
        letting it silently fall back to the connection's own id or misattribute a
        proposed source to a connection that doesn't exist.
        """
        ids = {c.id for c in self.connections}
        for conn in self.connections:
            target = conn.params.get("target_connection")
            if target is not None and target not in ids:
                raise ValueError(
                    f"connections[{conn.id}].params.target_connection={target!r} does not "
                    f"match any configured connection id; configured: {sorted(ids)}"
                )
        return self

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
            return (init_settings, YamlConfigSource(settings_cls, root / "canonic.yaml"))
        return (init_settings,)


def find_project_root() -> Path | None:
    """Walk up from cwd looking for canonic.yaml; return its directory or None."""
    current = Path.cwd()
    for directory in (current, *current.parents):
        if (directory / "canonic.yaml").exists():
            return directory
    return None


def dump_config(config: CanonicConfig, path: Path) -> None:
    """Write a validated CanonicConfig to path as canonic.yaml.

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
    """Create the canonic project skeleton under root; return the paths created.

    Idempotent — existing files and directories are left untouched. Scaffolds the
    four committed context directories (SPEC E1 §2), the restrictive-permission
    ``.canonic/`` local-state directory (§7), and a ``.gitignore`` covering
    ``.canonic/`` when none exists yet.
    """
    from canonic.contracts.loader import contracts_dir_scaffold

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


def load_config(path: Path) -> CanonicConfig:
    """Load and validate canonic.yaml at path, raising ConfigError on any problem."""
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
        raise ConfigError(f"unknown config version {version}, upgrade canonic")

    try:
        return CanonicConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = " → ".join(str(p) for p in first["loc"])
        raise ConfigError(f"{loc}: {first['msg']}") from exc
