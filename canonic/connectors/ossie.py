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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlglot import exp

from canonic.connectors.base import (
    AcquisitionTier,
    CandidateKind,
    Capability,
    ConnectorBase,
    ContractCandidate,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    DocEvidence,
    Health,
    JoinSpec,
    ReviewFlag,
    UsageEvidence,
    UsageHint,
)
from canonic.connectors.evidence import compute_doc_fingerprint
from canonic.connectors.ossie_sql import (
    Aggregate,
    AggregateKind,
    MetricClassifier,
    Ratio,
    Unclassified,
    render,
    select_expression,
    strip_qualifiers,
)
from canonic.exc import ConnectionError, UnsupportedSourceVersionError
from canonic.semantic.models import Additivity, Relationship

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


@dataclass
class _MappingResult:
    """What one model maps to: definitions, unmappable warnings, single-measure placement."""

    definitions: list[DefinitionEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metric_relations: dict[str, str] = field(default_factory=dict)


class _ModelMapper:
    """Maps one Ossie semantic model onto normalized definition evidence (§3.3 to §3.6)."""

    def __init__(
        self, model: OssieSemanticModel, *, source: str, target_dialect: str | None
    ) -> None:
        self._model = model
        self._source = source
        self._dialect = target_dialect
        self._root = f"ossie:{model.name}"
        self._datasets = {d.name: d for d in model.datasets if not d.is_query}
        self._result = _MappingResult()

    def map(self) -> _MappingResult:
        for dataset in self._model.datasets:
            if dataset.is_query:
                self._warn(f"dataset {dataset.name} is query-sourced")
                continue
            self._map_dataset(dataset)
        self._map_metrics()
        for relationship in self._model.relationships:
            self._map_relationship(relationship)
        return self._result

    def _warn(self, message: str) -> None:
        self._result.warnings.append(f"Ossie {self._model.name}: {message}; recorded as unmappable")

    def _emit(self, **fields: Any) -> None:
        evidence = DefinitionEvidence(
            source=self._source, acquisition_tier=AcquisitionTier.MODELING, **fields
        )
        semantic = evidence.model_dump(
            mode="json", exclude={"source", "native_ref", "acquisition_tier", "source_fingerprint"}
        )
        self._result.definitions.append(
            evidence.model_copy(update={"source_fingerprint": _fingerprint(semantic)})
        )

    def _map_dataset(self, dataset: OssieDataset) -> None:
        """One MODEL (description, grain), one ENTITY with a primary key, and the dimensions."""
        native_ref = f"{self._root}/{dataset.name}"
        grain = list(dataset.primary_key)
        self._emit(
            entity=dataset.source,
            entity_type=DefinitionEntityType.MODEL,
            grain=grain,
            description=dataset.description,
            native_ref=native_ref,
        )
        if grain:
            self._emit(
                entity=dataset.name,
                entity_type=DefinitionEntityType.ENTITY,
                references=[dataset.source],
                grain=grain,
                native_ref=native_ref,
            )
        for ossie_field in dataset.fields:
            if ossie_field.dimension is not None:
                self._map_dimension(dataset, ossie_field)

    def _map_dimension(self, dataset: OssieDataset, ossie_field: OssieField) -> None:
        """A bare column becomes ``column``, anything else ``expr``."""
        try:
            node = strip_qualifiers(self._select(ossie_field.expression))
        except ValueError as exc:
            self._warn(f"field {dataset.name}.{ossie_field.name}: {exc}")
            return
        bare = node.unnest() if isinstance(node, exp.Paren) else node
        is_column = isinstance(bare, exp.Column)
        self._emit(
            entity=ossie_field.name,
            entity_type=DefinitionEntityType.DIMENSION,
            column=bare.name if is_column else None,
            expr=None if is_column else render(node, self._dialect),
            is_time=_is_time(ossie_field),
            references=[dataset.source],
            description=ossie_field.description,
            aliases=_synonyms(ossie_field),
            native_ref=f"{self._root}/{dataset.name}/{ossie_field.name}",
        )

    def _map_metrics(self) -> None:
        """Classify every metric, then emit measures and contract candidates (§3.5).

        Ratio components reuse a single-aggregate metric with the same expression on the
        same dataset instead of emitting a duplicate measure.
        """
        classifier = MetricClassifier(
            self._column_owner,
            default_dataset=next(iter(self._datasets)) if len(self._datasets) == 1 else None,
        )
        shapes: list[tuple[OssieMetric, Aggregate | Ratio | Unclassified]] = []
        for metric in self._model.metrics:
            try:
                shapes.append((metric, classifier.classify(self._select(metric.expression))))
            except ValueError as exc:
                self._warn(f"metric {metric.name}: {exc}")
        measures: dict[tuple[str, str], str] = {}
        for metric, shape in shapes:
            if isinstance(shape, Aggregate) and shape.kind != AggregateKind.AVERAGE:
                measures.setdefault(self._measure_key(shape), metric.name)
        for metric, shape in shapes:
            if isinstance(shape, Ratio):
                self._map_ratio(metric, shape, measures)
            else:
                self._map_single(metric, shape)

    def _map_single(self, metric: OssieMetric, shape: Aggregate | Unclassified) -> None:
        dataset = self._datasets[shape.dataset]
        native_ref = f"{self._root}#metric/{metric.name}"
        if isinstance(shape, Unclassified):
            additivity, flags = None, [ReviewFlag.UNCLASSIFIED_AGGREGATION]
        elif shape.kind == AggregateKind.ADDITIVE:
            additivity, flags = Additivity.ADDITIVE, []
        elif shape.kind == AggregateKind.AVERAGE:
            additivity, flags = Additivity.NON_ADDITIVE, [ReviewFlag.AVG_SUGGESTS_RATIO]
        else:
            additivity, flags = Additivity.NON_ADDITIVE, []
        self._emit(
            entity=metric.name,
            entity_type=DefinitionEntityType.MEASURE,
            expr=render(shape.node, self._dialect),
            additivity=additivity,
            references=[dataset.source],
            description=metric.description,
            aliases=_synonyms(metric),
            review_flags=flags,
            native_ref=native_ref,
        )
        self._result.metric_relations[metric.name] = _relation_name(dataset.source)
        if isinstance(shape, Aggregate) and shape.kind == AggregateKind.DISTINCT_COUNT:
            self._emit(
                entity=metric.name,
                entity_type=DefinitionEntityType.METRIC,
                references=[dataset.source],
                contract_candidate=ContractCandidate(
                    kind=CandidateKind.DISTINCT_COUNT, measures=[metric.name]
                ),
                native_ref=native_ref,
            )

    def _map_ratio(
        self, metric: OssieMetric, ratio: Ratio, measures: dict[tuple[str, str], str]
    ) -> None:
        """Two component measures plus a ``ratio`` contract candidate, never a binding."""
        native_ref = f"{self._root}#metric/{metric.name}"
        names: list[str] = []
        for part, component in (("numerator", ratio.numerator), ("denominator", ratio.denominator)):
            key = self._measure_key(component)
            name = measures.get(key)
            if name is None:
                name = f"{metric.name}_{part}"
                measures[key] = name
                self._emit(
                    entity=name,
                    entity_type=DefinitionEntityType.MEASURE,
                    expr=key[1],
                    additivity=(
                        Additivity.ADDITIVE
                        if component.kind == AggregateKind.ADDITIVE
                        else Additivity.NON_ADDITIVE
                    ),
                    references=[self._datasets[component.dataset].source],
                    native_ref=f"{native_ref}/{part}",
                )
            names.append(name)
        sources = [self._datasets[c.dataset].source for c in (ratio.numerator, ratio.denominator)]
        self._emit(
            entity=metric.name,
            entity_type=DefinitionEntityType.METRIC,
            references=list(dict.fromkeys(sources)),
            description=metric.description,
            aliases=_synonyms(metric),
            contract_candidate=ContractCandidate(kind=CandidateKind.RATIO, measures=names),
            native_ref=native_ref,
        )

    def _map_relationship(self, rel: OssieRelationship) -> None:
        """A JOIN whose cardinality follows Ossie's many-to-one direction (§3.6).

        ``from`` is the many side and ``to`` the one side by definition, so the default is
        ``many_to_one``. Keys on both sides make it ``one_to_one``. Target columns that
        match no key of ``to`` keep ``many_to_one`` but are flagged for review.
        """
        source, target = self._datasets.get(rel.from_), self._datasets.get(rel.to)
        if source is None or target is None:
            self._warn(f"relationship {rel.name}: references an unknown or query-sourced dataset")
            return
        if not rel.from_columns or len(rel.from_columns) != len(rel.to_columns):
            self._warn(f"relationship {rel.name}: from_columns and to_columns do not pair up")
            return
        to_is_key = _is_key(rel.to_columns, target)
        if to_is_key and _is_key(rel.from_columns, source):
            relationship = Relationship.ONE_TO_ONE
        else:
            relationship = Relationship.MANY_TO_ONE
        left, right = _relation_name(source.source), _relation_name(target.source)
        on = " AND ".join(
            f"{left}.{f} = {right}.{t}"
            for f, t in zip(rel.from_columns, rel.to_columns, strict=True)
        )
        self._emit(
            entity=rel.name,
            entity_type=DefinitionEntityType.JOIN,
            references=[source.source, target.source],
            joins=[JoinSpec(left=left, right=right, relationship=relationship, on=on)],
            review_flags=[] if to_is_key else [ReviewFlag.JOIN_COLUMNS_NOT_A_KEY],
            native_ref=f"{self._root}#relationship/{rel.name}",
        )

    def _select(self, expression: OssieExpression) -> exp.Expression:
        return select_expression(
            [(d.dialect, d.expression) for d in expression.dialects], self._dialect
        )

    def _measure_key(self, aggregate: Aggregate) -> tuple[str, str]:
        return aggregate.dataset, render(aggregate.node, self._dialect)

    def _column_owner(self, column: exp.Column) -> str | None:
        """A qualified column belongs to the named dataset, a bare one to the only dataset
        declaring a field of that name."""
        if column.table:
            return column.table if column.table in self._datasets else None
        owners = [
            name
            for name, dataset in self._datasets.items()
            if any(f.name == column.name for f in dataset.fields)
        ]
        if len(owners) == 1:
            return owners[0]
        return next(iter(self._datasets)) if len(self._datasets) == 1 else None


_TEMPORAL_DATATYPES = frozenset({"Date", "Time", "DateTime", "DateTimeTz"})


def _is_time(ossie_field: OssieField) -> bool:
    """Explicit ``dimension.is_time`` wins, otherwise a temporal ``datatype`` implies it."""
    explicit = (ossie_field.dimension or {}).get("is_time")
    if explicit is not None:
        return bool(explicit)
    return ossie_field.datatype in _TEMPORAL_DATATYPES


def _synonyms(carrier: _AIContextCarrier) -> list[str]:
    return list(carrier.ai_context.synonyms) if carrier.ai_context else []


def _is_key(columns: list[str], dataset: OssieDataset) -> bool:
    """True when ``columns`` are exactly the primary key or one declared unique key."""
    keys = [dataset.primary_key, *dataset.unique_keys]
    return any(key and set(key) == set(columns) for key in keys)


class OssieConnector(ConnectorBase):
    """Definition + evidence connector for Apache Ossie semantic model files.

    Args:
        paths: Local files or glob patterns.  URLs are rejected for now.
        source: The primary ``target_connection`` id stamped on every evidence item.
        target_dialect: sqlglot dialect of the target connection. Decides which
            expression variant is used and what it is transpiled to. ``None`` falls back
            to the portable variants rendered in sqlglot's generic dialect.
    """

    def __init__(self, paths: list[str], *, source: str, target_dialect: str | None = None) -> None:
        self._paths = list(paths)
        self._source = source
        self._target_dialect = target_dialect

    def capabilities(self) -> list[Capability]:
        return [
            Capability.CAPABILITIES,
            Capability.TEST_CONNECTION,
            Capability.EXTRACT_DEFINITIONS,
            Capability.EXTRACT_EVIDENCE,
        ]

    async def test_connection(self) -> Health:
        """Verify the files parse with a supported version, and report unmappable objects."""
        try:
            loaded = self._load()
        except ConnectionError as exc:
            return Health(status="error", message=str(exc))
        versions = sorted({m.version for m in loaded})
        files = len({m.path for m in loaded})
        warnings = tuple(w for item in loaded for w in self._map(item).warnings)
        return Health(
            status="ok",
            message=f"{files} file(s), {len(loaded)} model(s), Ossie {', '.join(versions)}",
            warnings=warnings,
        )

    async def extract_definitions(self) -> DefinitionExtract:
        """Map datasets, dimensions, metrics and relationships to definition evidence.

        Raises :exc:`UnsupportedSourceVersionError` before producing anything when a
        file is out of the pinned range, so no partial ingest occurs (SPEC-E3 §6).
        Unmappable objects are logged as warnings naming the object, never dropped silently.
        """
        definitions: list[DefinitionEvidence] = []
        for item in self._load():
            result = self._map(item)
            for warning in result.warnings:
                logger.warning(warning)
            definitions.extend(result.definitions)
        return DefinitionExtract(definitions=definitions)

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Map ``ai_context.instructions`` on every object to one :class:`DocEvidence`."""
        observed_at = datetime.now(UTC)
        docs: list[DocEvidence | UsageEvidence] = []
        for item in self._load():
            placement = self._map(item).metric_relations
            for title, body, topic_refs, native_ref in self._instruction_targets(
                item.model, placement
            ):
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

    def _map(self, item: _LoadedModel) -> _MappingResult:
        return _ModelMapper(
            item.model, source=self._source, target_dialect=self._target_dialect
        ).map()

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

    def _instruction_targets(
        self, model: OssieSemanticModel, metric_relations: dict[str, str]
    ) -> list[tuple[str, str, list[str], str]]:
        """Every object carrying ``ai_context.instructions``: (title, body, topic_refs, ref).

        ``topic_refs`` are candidates in the ``{connection}.{source}[.{member}]`` form that
        knowledge resolution understands. A metric placed as a single measure is qualified
        by its relation, a ratio metric spans sources and keeps its bare name.
        """
        datasets = {d.name: d for d in model.datasets}

        def qualified(dataset_name: str, member: str | None = None) -> str:
            dataset = datasets.get(dataset_name)
            relation = _relation_name(dataset.source) if dataset else dataset_name
            base = f"{self._source}.{relation}"
            return f"{base}.{member}" if member else base

        def metric_ref(name: str) -> str:
            relation = metric_relations.get(name)
            return f"{self._source}.{relation}.{name}" if relation else name

        targets: list[tuple[str, str, list[str], str]] = []

        def add(carrier: _AIContextCarrier, title: str, refs: list[str], ref: str) -> None:
            if carrier.ai_context and carrier.ai_context.instructions:
                targets.append((title, carrier.ai_context.instructions.strip(), refs, ref))

        root = f"ossie:{model.name}"
        add(model, f"{model.name}", [], root)
        for dataset in model.datasets:
            ref = f"{root}/{dataset.name}"
            add(dataset, f"{model.name}: {dataset.name}", [qualified(dataset.name)], ref)
            for ossie_field in dataset.fields:
                add(
                    ossie_field,
                    f"{model.name}: {dataset.name}.{ossie_field.name}",
                    [qualified(dataset.name, ossie_field.name)],
                    f"{ref}/{ossie_field.name}",
                )
        for metric in model.metrics:
            add(
                metric,
                f"{model.name}: {metric.name}",
                [metric_ref(metric.name)],
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
