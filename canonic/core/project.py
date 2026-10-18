"""The project state a service serves from, and the reloader that keeps it current.

A long-running server loads ``canonic.yaml``, ``contracts/`` and ``semantics/`` once, so an
edit would have no effect until the process restarted. :class:`ProjectReloader` compares a
signature of those files before each request and rebuilds the state when it changed, the same
pattern the knowledge pages use.

The state itself is immutable. A reload builds a complete new :class:`ProjectState` and swaps
one reference, and a failed reload leaves the previous state in place, so a request never sees
a half-loaded project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from canonic.compiler.dialect import DIALECT_ADAPTERS, TYPE_TO_DIALECT, adapter_for
from canonic.config import CanonicConfig, Connection, load_config
from canonic.contracts import ContractResolver
from canonic.exc import CanonicError, ConfigError
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canonic.semantic.models import SemanticSource

__all__ = ["ProjectReloader", "ProjectSignature", "ProjectState", "ReloadFailure"]

logger = logging.getLogger(__name__)

#: One entry per watched file: its path, modification time and size.
ProjectSignature = tuple[tuple[str, int, int], ...]

_FILE_PATH_PARAMS: dict[str, str] = {
    "duckdb": "path",
    "sqlite": "path",
    "dbt": "manifest_path",
    "ossie": "paths",
}

#: What a reload can raise for a project that is invalid or changing under it: a coded error
#: from a loader, a config error, a validation error (a ``ValueError``), or a file that
#: disappeared between listing and reading.
_RELOAD_ERRORS = (CanonicError, ConfigError, ValueError, OSError)


def _dialect_for_type(connector_type: str) -> str | None:
    """Map a query connector's type string to a sqlglot dialect name.

    Returns ``None`` for a definitions/evidence-only connector type (dbt, looker,
    metabase, notion, ossie, url), those never serve a compiled query, so they get no entry
    in ``connection_dialects`` at all rather than a meaningless placeholder dialect.
    A project pairing a query connector with, say, a dbt companion connection (see
    examples/dutch-railway, jaffle-shop, ecommerce) is the normal case this must not
    break: only the query connector's type reaches :func:`adapter_for`.
    """
    dialect = TYPE_TO_DIALECT.get(connector_type, connector_type)
    if dialect not in DIALECT_ADAPTERS:
        return None
    return adapter_for(dialect).dialect


def _resolve_connection_paths(connections: list[Connection], root: Path) -> None:
    """Resolve relative file paths in file-based connections against the project root.

    Mutates params in-place so callers downstream always receive absolute paths,
    regardless of the process working directory. A list-valued param (Ossie ``paths``)
    has each entry resolved, glob patterns included.
    """
    for conn in connections:
        param_key = _FILE_PATH_PARAMS.get(conn.type)
        if param_key is None:
            continue
        raw = conn.params.get(param_key)
        if isinstance(raw, list):
            conn.params[param_key] = [_resolve_path(str(p), root) for p in raw]
        elif raw:
            conn.params[param_key] = _resolve_path(raw, root)


def _resolve_path(raw: str, root: Path) -> str:
    if raw.startswith(("http://", "https://")) or Path(raw).is_absolute():
        return raw
    return str(root / raw)


@dataclass(frozen=True)
class ProjectState:
    """Everything a request reads from the project, loaded together and never mutated."""

    config: CanonicConfig
    resolver: ContractResolver
    sources: list[SemanticSource]
    connection_dialects: dict[str, str]
    source_by_name: dict[str, SemanticSource]

    @classmethod
    def build(
        cls, config: CanonicConfig, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> ProjectState:
        """Derive the lookups that depend on the loaded parts."""
        # connection id → sqlglot dialect name, derived from connection types in config.
        # Definitions/evidence-only connections (dbt, looker, ...) get no entry.
        connection_dialects = {
            c.id: dialect
            for c in config.connections
            if (dialect := _dialect_for_type(c.type)) is not None
        }
        return cls(
            config=config,
            resolver=resolver,
            sources=sources,
            connection_dialects=connection_dialects,
            # name → source for fast lookup (sources have unique names within project)
            source_by_name={s.name: s for s in sources},
        )

    @classmethod
    def load(cls, root: Path) -> ProjectState:
        """Load config, contracts and semantic sources from a project root."""
        config = load_config(root / "canonic.yaml")
        _resolve_connection_paths(config.connections, root)
        return cls.build(config, ContractResolver.from_project(root), list_semantic_sources(root))


@dataclass(frozen=True)
class ReloadFailure:
    """A reload that did not validate: the files it was attempted for and why it failed."""

    signature: ProjectSignature
    message: str

    def warning(self) -> str:
        return f"project files changed but did not load, serving the previous state: {self.message}"


class ProjectReloader:
    """Rebuilds the project state when the files it was loaded from change.

    :meth:`refresh` is called before a request is served and returns the state that request
    should use. Only ``canonic.yaml``, ``contracts/`` and ``semantics/`` are watched.
    Connections are read from the new config too, but a connector already built for a request
    in flight is not torn down, and settings consumed at startup (the MCP auth mechanism, the
    event log location) need a restart.

    A change that fails validation is remembered by its signature. The previous state keeps
    serving, :attr:`failure` names the file, and the same broken files are not parsed again
    until they change.
    """

    def __init__(self, root: Path, state: ProjectState) -> None:
        self._root = root
        self._state = state
        self._signature = self._read_signature()
        self._failure: ReloadFailure | None = None

    @property
    def state(self) -> ProjectState:
        return self._state

    @property
    def failure(self) -> ReloadFailure | None:
        """The failed reload that is still outstanding, or ``None`` when the state is current."""
        return self._failure

    def refresh(self) -> ProjectState:
        """The current state, reloaded first if a watched file changed since the last call."""
        signature = self._read_signature()
        if signature == self._signature:
            self._failure = None
            return self._state
        if self._failure is not None and self._failure.signature == signature:
            return self._state
        try:
            state = ProjectState.load(self._root)
        except _RELOAD_ERRORS as exc:
            self._failure = ReloadFailure(signature=signature, message=str(exc))
            logger.warning("%s", self._failure.warning())
            return self._state
        self._state = state
        self._signature = signature
        self._failure = None
        logger.info("project reloaded after a file change: %s", self._root)
        return state

    def _read_signature(self) -> ProjectSignature:
        entries: list[tuple[str, int, int]] = []
        for path in self._watched_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((str(path.relative_to(self._root)), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def _watched_files(self) -> list[Path]:
        files = [self._root / "canonic.yaml"]
        for directory in ("contracts", "semantics"):
            base = self._root / directory
            if base.is_dir():
                files.extend(sorted(base.rglob("*.yaml")))
        return files
