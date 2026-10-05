"""Apache Ossie connector: parse Ossie semantic model files into normalized evidence.

Ossie (incubating, formerly Open Semantic Interchange) is a vendor-neutral YAML/JSON
interchange format for semantic models.  This connector reads it as a *boundary*
format (AMENDMENT-ossie-interchange §3): parsing is deterministic structured parsing,
the same rule as dbt (SPEC-E3 §5.1), with no fetch/extract split and no LLM.

Ossie has no notion of a connection.  Every dataset is bound to one primary
``target_connection``, which is stamped as ``source`` on every item so proposals land at
the same ``semantics/<target>/<name>.yaml`` path as that connection's live introspection.
No :class:`RelationSchema` is emitted: column types always come from the live schema.

Version pinning (SPEC-E3 §6): two document shapes are accepted, and anything else fails
``test_connection`` with :exc:`UnsupportedSourceVersionError` and ingests nothing.

- ``0.1.x``: a root ``semantic_model`` array, one entry per model.
- ``0.2.0.dev0``: one flat model per document at the root.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ConnectorBase,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    DocEvidence,
    Health,
    UsageEvidence,
    UsageHint,
)
from canonic.connectors.evidence import compute_doc_fingerprint
from canonic.exc import ConnectionError, UnsupportedSourceVersionError

logger = logging.getLogger(__name__)

__all__ = [
    "SUPPORTED_VERSIONS",
    "OssieAIContext",
    "OssieConnector",
    "OssieDataset",
    "OssieSemanticModel",
]

# 0.1.x documents wrap their models in a ``semantic_model`` array; 0.2.0.dev0 is flat.
_ARRAY_SHAPE_VERSION = re.compile(r"0\.1\.\d+")
_FLAT_SHAPE_VERSIONS: frozenset[str] = frozenset({"0.2.0.dev0"})
SUPPORTED_VERSIONS = "0.1.x (semantic_model array), 0.2.0.dev0 (flat document)"

_URL_PREFIXES = ("http://", "https://")
_GLOB_CHARS = frozenset("*?[")
# A dataset ``source`` that is a query rather than a relation (AMENDMENT §3.4, §8.1).
_QUERY_SOURCE = re.compile(r"^\s*\(?\s*(select|with)\b", re.IGNORECASE)


class OssieAIContext(BaseModel):
    """Normalized ``ai_context``: the string form becomes ``instructions``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    instructions: str | None = None
    synonyms: list[str] = []
    examples: list[str] = []


def _normalize_ai_context(value: Any) -> Any:
    """Accept both the string and the structured object form of ``ai_context``."""
    if isinstance(value, str):
        return {"instructions": value}
    return value


class OssieDialectExpression(BaseModel):
    """One dialect-specific variant of an expression."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    dialect: str
    expression: str


class OssieExpression(BaseModel):
    """A multi-dialect expression (``expression.dialects[]``)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    dialects: list[OssieDialectExpression] = []


class OssieCustomExtension(BaseModel):
    """A vendor extension, recorded but never interpreted on import."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    vendor_name: str
    data: str = ""


class _AIContextCarrier(BaseModel):
    """Shared ``ai_context`` / ``custom_extensions`` handling for every Ossie object."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ai_context: OssieAIContext | None = None
    custom_extensions: list[OssieCustomExtension] = []

    @field_validator("ai_context", mode="before")
    @classmethod
    def _coerce_ai_context(cls, value: Any) -> Any:
        return _normalize_ai_context(value)


class OssieField(_AIContextCarrier):
    """A row-level field of a dataset."""

    name: str
    expression: OssieExpression
    dimension: dict[str, Any] | None = None
    label: str | None = None
    description: str | None = None
    datatype: str | None = None


class OssieDataset(_AIContextCarrier):
    """A logical dataset (fact or dimension table)."""

    name: str
    source: str
    primary_key: list[str] = []
    unique_keys: list[list[str]] = []
    description: str | None = None
    fields: list[OssieField] = []

    @property
    def is_query(self) -> bool:
        """True when ``source`` is a SQL query rather than a physical relation."""
        return bool(_QUERY_SOURCE.match(self.source))


class OssieRelationship(_AIContextCarrier):
    """A foreign-key relationship: ``from`` is the many side, ``to`` the one side."""

    name: str
    from_: str = Field(alias="from")
    to: str
    from_columns: list[str]
    to_columns: list[str]


class OssieMetric(_AIContextCarrier):
    """A model-level aggregate metric, possibly spanning several datasets."""

    name: str
    expression: OssieExpression
    description: str | None = None
    datatype: str | None = None


class OssieSemanticModel(_AIContextCarrier):
    """One semantic model, independent of the document shape it was read from."""

    name: str
    description: str | None = None
    datasets: list[OssieDataset] = []
    relationships: list[OssieRelationship] = []
    metrics: list[OssieMetric] = []


@dataclass(frozen=True)
class _LoadedModel:
    """A parsed model plus where it came from."""

    path: Path
    version: str
    model: OssieSemanticModel


def _fingerprint(payload: dict[str, Any]) -> str:
    """Stable sha256 over a definition's semantic fields (same format as the dbt connector)."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _relation_name(source: str) -> str:
    """Short relation name, the key the builder uses to match live introspection."""
    return source.split(".")[-1]


def _unsupported(version: str, path: Path) -> UnsupportedSourceVersionError:
    return UnsupportedSourceVersionError(
        f"Ossie spec ({path})", detected=version or "unknown", supported=SUPPORTED_VERSIONS
    )


def _models_from_document(raw: Any, path: Path) -> list[_LoadedModel]:
    """Split one parsed document into its models, enforcing the version pin first.

    Raises:
        UnsupportedSourceVersionError: The declared version is outside the pin, or the
            document shape does not match its declared version.
        ConnectionError: The document is not a mapping or a model fails validation.
    """
    if not isinstance(raw, dict):
        raise ConnectionError(f"{path}: an Ossie document must be a mapping at the root")
    version = str(raw.get("version") or "")
    if "semantic_model" in raw:
        if not _ARRAY_SHAPE_VERSION.fullmatch(version):
            raise _unsupported(version, path)
        entries = raw["semantic_model"]
        if not isinstance(entries, list):
            raise ConnectionError(f"{path}: semantic_model must be a list")
    else:
        if version not in _FLAT_SHAPE_VERSIONS:
            raise _unsupported(version, path)
        entries = [raw]
    try:
        return [
            _LoadedModel(path=path, version=version, model=OssieSemanticModel.model_validate(e))
            for e in entries
        ]
    except ValidationError as exc:
        raise ConnectionError(f"{path}: invalid Ossie semantic model: {exc}") from exc


class OssieConnector(ConnectorBase):
    """Definition + evidence connector for Apache Ossie semantic model files.

    Args:
        paths: Local files or glob patterns.  URLs are rejected for now.
        source: The primary ``target_connection`` id stamped on every evidence item.
    """

    def __init__(self, paths: list[str], *, source: str) -> None:
        self._paths = list(paths)
        self._source = source

    def capabilities(self) -> list[Capability]:
        return [
            Capability.CAPABILITIES,
            Capability.TEST_CONNECTION,
            Capability.EXTRACT_DEFINITIONS,
            Capability.EXTRACT_EVIDENCE,
        ]

    async def test_connection(self) -> Health:
        """Verify the files are readable, parse, and declare a supported Ossie version."""
        try:
            loaded = self._load()
        except ConnectionError as exc:
            return Health(status="error", message=str(exc))
        versions = sorted({m.version for m in loaded})
        files = len({m.path for m in loaded})
        warnings = tuple(self._query_dataset_warnings(loaded))
        return Health(
            status="ok",
            message=f"{files} file(s), {len(loaded)} model(s), Ossie {', '.join(versions)}",
            warnings=warnings,
        )

    async def extract_definitions(self) -> DefinitionExtract:
        """Map datasets to MODEL and ENTITY definition evidence.

        Raises :exc:`UnsupportedSourceVersionError` before producing anything when a
        file is out of the pinned range, so no partial ingest occurs (SPEC-E3 §6).
        """
        loaded = self._load()
        for warning in self._query_dataset_warnings(loaded):
            logger.warning(warning)
        definitions: list[DefinitionEvidence] = []
        for item in loaded:
            for dataset in item.model.datasets:
                if not dataset.is_query:
                    definitions.extend(self._dataset_definitions(item.model, dataset))
        return DefinitionExtract(definitions=definitions)

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Map ``ai_context.instructions`` on every object to one :class:`DocEvidence`."""
        loaded = self._load()
        observed_at = datetime.now(UTC)
        docs: list[DocEvidence | UsageEvidence] = []
        for item in loaded:
            for title, body, topic_refs, native_ref in self._instruction_targets(item.model):
                docs.append(
                    DocEvidence(
                        source=self._source,
                        title=title,
                        body=body,
                        topic_refs=topic_refs,
                        usage_hint=UsageHint.REFERENCE,
                        native_ref=native_ref,
                        source_fingerprint=compute_doc_fingerprint(
                            title, body, UsageHint.REFERENCE.value, topic_refs
                        ),
                        observed_at=observed_at,
                    )
                )
        return docs

    def _resolve_files(self) -> list[Path]:
        """Expand configured paths and globs into a sorted, de-duplicated file list."""
        files: set[Path] = set()
        for entry in self._paths:
            if entry.startswith(_URL_PREFIXES):
                raise ConnectionError(
                    f"Ossie URL sources are not supported yet: {entry}; download the file "
                    "and reference it by path"
                )
            if _GLOB_CHARS & set(entry):
                matches = [Path(m) for m in glob.glob(entry, recursive=True)]
                if not matches:
                    raise ConnectionError(f"no Ossie files match {entry!r}")
                files.update(m for m in matches if m.is_file())
            else:
                path = Path(entry)
                if not path.is_file():
                    raise ConnectionError(f"Ossie file not found: {path}")
                files.add(path)
        return sorted(files)

    def _load(self) -> list[_LoadedModel]:
        """Read and validate every configured file; all-or-nothing."""
        yaml = YAML(typ="safe")
        loaded: list[_LoadedModel] = []
        for path in self._resolve_files():
            try:
                raw = yaml.load(path.read_text(encoding="utf-8"))
            except (OSError, YAMLError) as exc:
                raise ConnectionError(f"cannot read Ossie file {path}: {exc}") from exc
            loaded.extend(_models_from_document(raw, path))
        return loaded

    @staticmethod
    def _query_dataset_warnings(loaded: list[_LoadedModel]) -> list[str]:
        """Query-sourced datasets have no relation to bind to and stay unmappable (§8.1)."""
        return [
            f"Ossie dataset {item.model.name}/{dataset.name} is query-sourced; "
            "recorded as unmappable"
            for item in loaded
            for dataset in item.model.datasets
            if dataset.is_query
        ]

    def _dataset_definitions(
        self, model: OssieSemanticModel, dataset: OssieDataset
    ) -> list[DefinitionEvidence]:
        """One MODEL (description) and, with a primary key, one ENTITY (grain) per dataset."""
        native_ref = f"ossie:{model.name}/{dataset.name}"
        grain = list(dataset.primary_key)
        definitions = [
            DefinitionEvidence(
                source=self._source,
                entity=dataset.source,
                entity_type=DefinitionEntityType.MODEL,
                grain=grain,
                description=dataset.description,
                native_ref=native_ref,
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=_fingerprint(
                    {
                        "entity": dataset.source,
                        "entity_type": "model",
                        "grain": grain,
                        "description": dataset.description,
                    }
                ),
            )
        ]
        if grain:
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=dataset.name,
                    entity_type=DefinitionEntityType.ENTITY,
                    references=[dataset.source],
                    grain=grain,
                    native_ref=native_ref,
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=_fingerprint(
                        {"entity": dataset.name, "entity_type": "entity", "grain": grain}
                    ),
                )
            )
        return definitions

    def _instruction_targets(
        self, model: OssieSemanticModel
    ) -> list[tuple[str, str, list[str], str]]:
        """Every object carrying ``ai_context.instructions``: (title, body, topic_refs, ref).

        ``topic_refs`` are candidates in the ``{connection}.{source}[.{member}]`` form that
        knowledge resolution understands.  Metric refs carry only the metric name until
        metric placement exists, so resolution matches them by measure name.
        """
        datasets = {d.name: d for d in model.datasets}

        def qualified(dataset_name: str, member: str | None = None) -> str:
            dataset = datasets.get(dataset_name)
            relation = _relation_name(dataset.source) if dataset else dataset_name
            base = f"{self._source}.{relation}"
            return f"{base}.{member}" if member else base

        targets: list[tuple[str, str, list[str], str]] = []

        def add(carrier: _AIContextCarrier, title: str, refs: list[str], ref: str) -> None:
            if carrier.ai_context and carrier.ai_context.instructions:
                targets.append((title, carrier.ai_context.instructions.strip(), refs, ref))

        root = f"ossie:{model.name}"
        add(model, f"{model.name}", [], root)
        for dataset in model.datasets:
            ref = f"{root}/{dataset.name}"
            add(dataset, f"{model.name}: {dataset.name}", [qualified(dataset.name)], ref)
            for field in dataset.fields:
                add(
                    field,
                    f"{model.name}: {dataset.name}.{field.name}",
                    [qualified(dataset.name, field.name)],
                    f"{ref}/{field.name}",
                )
        for metric in model.metrics:
            add(
                metric,
                f"{model.name}: {metric.name}",
                [metric.name],
                f"{root}#metric/{metric.name}",
            )
        for rel in model.relationships:
            add(
                rel,
                f"{model.name}: {rel.name}",
                [qualified(rel.from_), qualified(rel.to)],
                f"{root}#relationship/{rel.name}",
            )
        return targets
