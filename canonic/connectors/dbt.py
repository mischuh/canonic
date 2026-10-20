"""dbt manifest connector — parse compiled manifest.json → normalized evidence (SPEC-E3 §4, S1).

Emits :class:`RelationSchema` at acquisition tier ``modeling`` (ladder tier 2) and
:class:`DefinitionEvidence` for measures, dimensions, joins, and entities.  No dbt-specific
structure crosses the boundary into E4; ``native_ref`` carries the dbt ``unique_id`` as the
sole back-pointer for provenance (SPEC-E3 §3.1).

Version pinning: the manifest schema version is detected and enforced at
``test_connection``/extract time (SPEC-E3 §6, S5).  A manifest below schema ``v10``
(dbt Core 1.6+) fails with a clear :exc:`UnsupportedSourceVersionError` and ingests
nothing — partial ingest from an incompatible manifest is never silently accepted (PRD FR-2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from canonic.connectors.base import (
    AcquisitionTier,
    CandidateInput,
    CandidateKind,
    Capability,
    ColumnInfo,
    ConnectorBase,
    ContractCandidate,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    ForeignKey,
    ForeignKeyRef,
    Health,
    JoinSpec,
    RelationSchema,
    ReviewFlag,
    compute_fingerprint,
)
from canonic.contracts.models import derived_input_names
from canonic.exc import UnsupportedSourceVersionError
from canonic.semantic.duration import Duration, DurationUnit
from canonic.semantic.models import Additivity, Relationship

logger = logging.getLogger(__name__)

__all__ = ["DbtConnector"]

# Minimum supported dbt manifest schema version (dbt Core 1.6+ emits v10+).
MIN_MANIFEST_SCHEMA_VERSION = 10

# dbt aggregate function name → Canonic Additivity.  All others are recorded
# with a warning and omitted (additivity=None → "unknown" per SPEC-E3 §3.1).
_ADDITIVE_AGGS: frozenset[str] = frozenset({"sum", "count", "min", "max"})
_NON_ADDITIVE_AGGS: frozenset[str] = frozenset(
    {"average", "count_distinct", "median", "percentile", "percentile_cont", "percentile_disc"}
)

# dbt entity type → join relationship direction (primary entity is the "to" side).
# foreign → many_to_one; primary / natural → one_to_one; unique → one_to_one.
_ENTITY_TYPE_RELATIONSHIP: dict[str, Relationship] = {
    "primary": Relationship.ONE_TO_ONE,
    "natural": Relationship.ONE_TO_ONE,
    "unique": Relationship.ONE_TO_ONE,
    "foreign": Relationship.MANY_TO_ONE,
}

# Normalized type mapping for dbt column data_type values (mirrors E2 §2.1).
_DBT_TYPE_MAP: dict[str, str] = {
    "text": "string",
    "varchar": "string",
    "character varying": "string",
    "char": "string",
    "string": "string",
    "uuid": "string",
    "name": "string",
    "integer": "int",
    "int": "int",
    "int2": "int",
    "int4": "int",
    "int8": "int",
    "smallint": "int",
    "bigint": "int",
    "numeric": "decimal",
    "decimal": "decimal",
    "real": "float",
    "float": "float",
    "float4": "float",
    "float8": "float",
    "double precision": "float",
    "double": "float",
    "boolean": "bool",
    "bool": "bool",
    "date": "date",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamptz": "timestamp",
    "datetime": "timestamp",
    "json": "json",
    "jsonb": "json",
    "variant": "json",
    "object": "json",
    "array": "json",
}


def _normalize_type(raw: str | None, relation: str, column: str) -> str:
    """Map a dbt column data_type to the normalized type set.

    Missing or unmappable types are recorded as ``json`` with a WARNING
    — never dropped silently and never cause a crash (SPEC-E3 §4, AC2).
    """
    if not raw:
        logger.warning("missing data_type on %s.%s recorded as json", relation, column)
        return "json"
    t = re.sub(r"\(.*\)", "", raw.strip().lower()).strip()
    mapped = _DBT_TYPE_MAP.get(t)
    if mapped is None:
        logger.warning("unmapped dbt type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    return mapped


def _render_aggregate(agg_type: str | None, expr: str) -> str:
    """Render a dbt aggregate over ``expr`` as SQL.

    dbt aggregate names are not all SQL functions: ``average`` is ``avg`` and
    ``count_distinct`` is ``count(distinct …)``. Other names render as ``name(expr)``.
    """
    if not agg_type:
        return expr
    t = agg_type.strip().lower()
    if t == "count_distinct":
        return f"count(distinct {expr})"
    if t == "average":
        return f"avg({expr})"
    return f"{t}({expr})"


def _review_flags_for(agg_type: str | None) -> list[ReviewFlag]:
    """Flag aggregates that need a human look after import.

    A plain ``average`` is not additive and usually wants a ``ratio`` binding instead.
    """
    if agg_type and agg_type.strip().lower() == "average":
        return [ReviewFlag.AVG_SUGGESTS_RATIO]
    return []


def _additivity_for(agg_type: str | None, relation: str, measure: str) -> Additivity | None:
    """Map a dbt aggregate type to Additivity.

    Returns ``None`` (SPEC-E3 "unknown") for unrecognized aggregation types and
    emits a WARNING so the caller can skip or record the item (never silently drop).
    """
    if not agg_type:
        return None
    t = agg_type.strip().lower()
    if t in _ADDITIVE_AGGS:
        return Additivity.ADDITIVE
    if t in _NON_ADDITIVE_AGGS:
        return Additivity.NON_ADDITIVE
    logger.warning(
        "unrecognized dbt agg_type %r on %s.%s; additivity recorded as unknown",
        agg_type,
        relation,
        measure,
    )
    return None


def _agg_time_dimensions(semantic_models: list[dict[str, Any]]) -> dict[str, str]:
    """Measure name to the time dimension MetricFlow aggregates it along.

    A measure's own ``agg_time_dimension`` wins over its semantic model's default.
    """
    result: dict[str, str] = {}
    for sm in semantic_models:
        default = (sm.get("defaults") or {}).get("agg_time_dimension")
        for measure in sm.get("measures", []):
            dimension = measure.get("agg_time_dimension") or default
            if dimension:
                result[measure.get("name", "")] = dimension
    return result


def _measure_entities(semantic_models: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Measure name to the entities of its semantic model, each to the column it reads."""
    result: dict[str, dict[str, str]] = {}
    for sm in semantic_models:
        entities = {
            str(e.get("name", "")): str(e.get("expr") or e.get("name", ""))
            for e in sm.get("entities", [])
        }
        for measure in sm.get("measures", []):
            result[measure.get("name", "")] = entities
    return result


def _definition_fingerprint(payload: dict[str, Any]) -> str:
    """Stable sha256 over a definition's semantic fields (mirrors compute_fingerprint format)."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _parse_manifest_version(schema_url: str | None) -> str | None:
    """Extract 'vN' from a dbt manifest schema URL, e.g. '.../manifest/v11.json' → 'v11'."""
    if not schema_url:
        return None
    m = re.search(r"/manifest/(v\d+)\.json", schema_url)
    return m.group(1) if m else None


class DbtConnector(ConnectorBase):
    """Definition connector for a compiled dbt ``manifest.json`` (SPEC-E3 §4, dbt Core 1.6+).

    Constructed directly from a manifest path (no live database connection).
    Version detection and enforcement run at both ``test_connection`` and
    ``extract_definitions`` time; a manifest below schema ``v10`` (dbt Core 1.6+) is
    rejected with :exc:`UnsupportedSourceVersionError` and ingests nothing (S5).

    Args:
        manifest_path: Path to the compiled ``manifest.json``.
        source: Connection id used to stamp evidence items (default ``"dbt"``).
    """

    def __init__(self, manifest_path: str | Path, *, source: str = "dbt") -> None:
        self._path = Path(manifest_path)
        self._source = source
        #: MetricFlow simple metric name to its measure, filled when definitions are extracted.
        self._metric_measures: dict[str, str] = {}
        #: Measure name to its semantic model's entities and their columns, filled likewise.
        self._measure_entities: dict[str, dict[str, str]] = {}

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_DEFINITIONS]

    def _assert_supported_version(self, metadata: dict[str, Any]) -> str:
        """Enforce the pinned manifest schema floor; raise if out of range.

        Returns the detected ``vN`` schema label on success.
        """
        schema_version = _parse_manifest_version(metadata.get("dbt_schema_version"))
        if schema_version is None or int(schema_version[1:]) < MIN_MANIFEST_SCHEMA_VERSION:
            raise UnsupportedSourceVersionError(
                "dbt manifest schema",
                detected=schema_version or "unknown",
                supported=f"v{MIN_MANIFEST_SCHEMA_VERSION}+ (dbt Core 1.6+)",
            )
        return schema_version

    async def test_connection(self) -> Health:
        """Verify the manifest exists, parses as JSON, and meets the pinned schema floor."""
        if not self._path.exists():
            return Health(status="error", message=f"manifest not found: {self._path}")
        try:
            manifest = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            return Health(status="error", message=f"cannot read manifest: {exc}")
        metadata = manifest.get("metadata", {})
        try:
            schema_version = self._assert_supported_version(metadata)
        except UnsupportedSourceVersionError as exc:
            return Health(status="error", message=str(exc))
        dbt_version = metadata.get("dbt_version", "unknown")
        return Health(status="ok", message=f"dbt {dbt_version}, manifest {schema_version}")

    async def extract_definitions(self) -> DefinitionExtract:
        """Parse the manifest and return normalized schemas + definition evidence.

        Enforces the pinned schema floor first, raising :exc:`UnsupportedSourceVersionError`
        on an out-of-range manifest so no partial ingest occurs (SPEC-E3 §6, S5).
        """
        manifest = json.loads(self._path.read_text())
        self._assert_supported_version(manifest.get("metadata", {}))
        nodes: dict[str, Any] = manifest.get("nodes", {})
        raw_sm = manifest.get("semantic_models", {})
        semantic_models: list[dict[str, Any]] = (
            list(raw_sm.values()) if isinstance(raw_sm, dict) else list(raw_sm)
        )
        metrics: dict[str, Any] = manifest.get("metrics", {})

        relations: list[RelationSchema] = []
        definitions: list[DefinitionEvidence] = []

        # --- models → RelationSchema + MODEL DefinitionEvidence ---
        model_relations: dict[str, str] = {}
        for node in nodes.values():
            if node.get("resource_type") != "model":
                continue
            rel = self._extract_model(node, relations, definitions)
            if rel:
                model_relations[node["unique_id"]] = rel

        # --- semantic_models → ENTITY / JOIN / MEASURE / DIMENSION evidence ---
        for sm in semantic_models:
            self._extract_semantic_model(sm, definitions)

        # --- metrics → MEASURE evidence ---
        agg_time_dimensions = _agg_time_dimensions(semantic_models)
        self._measure_entities = _measure_entities(semantic_models)
        self._metric_measures = {
            str(m.get("name", "")): _measure_name(m.get("type_params", {}))
            for m in metrics.values()
            if m.get("type") == "simple"
        }
        for metric in metrics.values():
            self._extract_metric(metric, definitions, agg_time_dimensions)

        return DefinitionExtract(relations=relations, definitions=definitions)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fq_relation(self, node: dict[str, Any]) -> str:
        """Build a fully-qualified relation name from node metadata."""
        schema = node.get("schema", "")
        alias = node.get("alias") or node.get("name", "")
        db = node.get("database")
        if db:
            return f"{db}.{schema}.{alias}"
        return f"{schema}.{alias}" if schema else alias

    def _extract_model(
        self,
        node: dict[str, Any],
        relations: list[RelationSchema],
        definitions: list[DefinitionEvidence],
    ) -> str | None:
        """Build a RelationSchema + MODEL DefinitionEvidence for one dbt model node."""
        unique_id: str = node.get("unique_id", "")
        fq = self._fq_relation(node)
        raw_columns: dict[str, Any] = node.get("columns", {})

        columns: list[ColumnInfo] = []
        for col_name, col_meta in raw_columns.items():
            norm_type = _normalize_type(col_meta.get("data_type"), fq, col_name)
            columns.append(ColumnInfo(name=col_name, type=norm_type, nullable=True))

        # Primary key from constraints (dbt 1.6+) or node-level primary_key list
        primary_key: list[str] = []
        for col_name, col_meta in raw_columns.items():
            for constraint in col_meta.get("constraints", []):
                if constraint.get("type") == "primary_key":
                    primary_key.append(col_name)
        if not primary_key:
            primary_key = node.get("primary_key", [])

        # Foreign keys from relationship constraints
        foreign_keys: list[ForeignKey] = []
        for col_name, col_meta in raw_columns.items():
            for constraint in col_meta.get("constraints", []):
                if constraint.get("type") == "foreign_key":
                    ref_model = constraint.get("to", "")
                    ref_columns = constraint.get("to_columns", [col_name])
                    if ref_model:
                        foreign_keys.append(
                            ForeignKey(
                                columns=[col_name],
                                references=ForeignKeyRef(relation=ref_model, columns=ref_columns),
                            )
                        )

        fingerprint = compute_fingerprint(columns, primary_key, foreign_keys)
        mat = node.get("config", {}).get("materialized", "table")
        kind: str = "view" if mat == "view" else "table"

        relations.append(
            RelationSchema(
                connection=self._source,
                relation=fq,
                kind=kind,  # type: ignore[arg-type]
                columns=columns,
                primary_key=primary_key,
                foreign_keys=foreign_keys,
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=fingerprint,
            )
        )

        description = node.get("description") or None
        fp = _definition_fingerprint({"entity": fq, "entity_type": "model", "grain": primary_key})
        definitions.append(
            DefinitionEvidence(
                source=self._source,
                entity=fq,
                entity_type=DefinitionEntityType.MODEL,
                grain=primary_key,
                description=description,
                native_ref=unique_id,
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=fp,
            )
        )
        return fq

    def _extract_semantic_model(
        self, sm: dict[str, Any], definitions: list[DefinitionEvidence]
    ) -> None:
        """Emit definition evidence from one dbt semantic model block."""
        sm_name: str = sm.get("name", "")
        unique_id: str = sm.get("unique_id", sm_name)
        description = sm.get("description") or None

        # Resolve the backing model relation name
        model_ref: dict[str, Any] = sm.get("model", {})
        ref_name: str = (
            model_ref.get("ref_name", "") if isinstance(model_ref, dict) else str(model_ref)
        )
        node_relation = ref_name  # best-effort; fully qualified name may not be available

        entities: list[dict[str, Any]] = sm.get("entities", [])
        grain: list[str] = [
            e.get("expr") or e.get("name", "")
            for e in entities
            if e.get("type") in ("primary", "natural", "unique")
        ]

        # ENTITY definition (grain carrier)
        fp = _definition_fingerprint({"entity": sm_name, "entity_type": "entity", "grain": grain})
        definitions.append(
            DefinitionEvidence(
                source=self._source,
                entity=sm_name,
                entity_type=DefinitionEntityType.ENTITY,
                grain=grain,
                references=[node_relation] if node_relation else [],
                description=description,
                native_ref=unique_id,
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=fp,
            )
        )

        # JOIN definitions (from foreign entities)
        for entity in entities:
            etype = entity.get("type", "")
            if etype not in _ENTITY_TYPE_RELATIONSHIP or etype in ("primary", "natural", "unique"):
                continue
            relationship = _ENTITY_TYPE_RELATIONSHIP[etype]
            join_col = entity.get("expr") or entity.get("name", "")
            join_spec = JoinSpec(
                left=f"{sm_name}.{join_col}", right=join_col, relationship=relationship
            )
            fp = _definition_fingerprint(
                {"entity": entity.get("name", ""), "entity_type": "join", "left": join_spec.left}
            )
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=entity.get("name", ""),
                    entity_type=DefinitionEntityType.JOIN,
                    joins=[join_spec],
                    native_ref=f"{unique_id}#{entity.get('name', '')}",
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
                )
            )

        # MEASURE definitions
        for measure in sm.get("measures", []):
            m_name: str = measure.get("name", "")
            agg: str | None = measure.get("agg") or measure.get("agg_type")
            non_additive_dim: dict[str, Any] | None = measure.get("non_additive_dimension")

            additivity = _additivity_for(agg, sm_name, m_name)
            if non_additive_dim:
                additivity = Additivity.SEMI_ADDITIVE

            # Store the full aggregation expression so the builder can use it verbatim
            # in the semantic source (e.g. "count(order_id)" not just "order_id").
            raw_expr = measure.get("expr") or m_name
            expr = _render_aggregate(agg, raw_expr)
            refs = [node_relation] if node_relation else []
            fp = _definition_fingerprint({"entity": m_name, "entity_type": "measure", "expr": expr})
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=m_name,
                    entity_type=DefinitionEntityType.MEASURE,
                    expr=expr,
                    additivity=additivity,
                    references=refs,
                    review_flags=_review_flags_for(agg),
                    native_ref=f"{unique_id}#{m_name}",
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
                )
            )
            if agg and agg.strip().lower() == "count_distinct" and not non_additive_dim:
                # A distinct count only compiles through a ``distinct_count`` binding.
                definitions.append(
                    DefinitionEvidence(
                        source=self._source,
                        entity=m_name,
                        entity_type=DefinitionEntityType.METRIC,
                        references=refs,
                        contract_candidate=ContractCandidate(
                            kind=CandidateKind.DISTINCT_COUNT, measures=[m_name]
                        ),
                        native_ref=f"{unique_id}#{m_name}",
                        acquisition_tier=AcquisitionTier.MODELING,
                        source_fingerprint=_definition_fingerprint(
                            {"entity": m_name, "entity_type": "metric", "expr": expr}
                        ),
                    )
                )

        # DIMENSION definitions
        for dim in sm.get("dimensions", []):
            d_name: str = dim.get("name", "")
            expr = dim.get("expr") or d_name
            fp = _definition_fingerprint(
                {"entity": d_name, "entity_type": "dimension", "expr": expr}
            )
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=d_name,
                    entity_type=DefinitionEntityType.DIMENSION,
                    expr=expr,
                    references=[node_relation] if node_relation else [],
                    native_ref=f"{unique_id}#{d_name}",
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
                )
            )

    def _extract_metric(
        self,
        metric: dict[str, Any],
        definitions: list[DefinitionEvidence],
        agg_time_dimensions: dict[str, str] | None = None,
    ) -> None:
        """Emit a MEASURE DefinitionEvidence for one dbt metric.

        ``agg_time_dimensions`` maps a measure name to the time dimension MetricFlow
        aggregates it along, which is what a ``cumulative`` metric accumulates over.
        """
        unique_id: str = metric.get("unique_id", "")
        name: str = metric.get("name", "")
        description = metric.get("description") or None

        # Simple/single metrics carry a type and a measure reference
        metric_type: str = metric.get("type", "")
        type_params: dict[str, Any] = metric.get("type_params", {})

        measure_ref: str = ""
        if isinstance(type_params.get("measure"), dict):
            measure_ref = type_params["measure"].get("name", "")
        elif isinstance(type_params.get("measure"), str):
            measure_ref = type_params["measure"]

        # MetricFlow "simple" metrics are backed by a semantic-model measure that is
        # already emitted by _extract_semantic_model; emitting MEASURE here would
        # duplicate it with wrong references (the measure name, not the source table).
        # Legacy dbt metric types (sum/count/…) predate semantic models and carry the
        # aggregation definition directly in the metric, so emit MEASURE for those.
        _LEGACY_METRIC_TYPES: frozenset[str] = frozenset({"sum", "count", "average", "min", "max"})
        if metric_type in _LEGACY_METRIC_TYPES:
            additivity = _additivity_for(metric_type, "metric", name)
            expr = measure_ref or name
            refs = list(filter(None, [measure_ref]))
            fp = _definition_fingerprint({"entity": name, "entity_type": "measure", "expr": expr})
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=name,
                    entity_type=DefinitionEntityType.MEASURE,
                    expr=expr,
                    additivity=additivity,
                    references=refs,
                    description=description,
                    review_flags=_review_flags_for(metric_type),
                    native_ref=unique_id,
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
                )
            )
        elif metric_type == "cumulative":
            self._extract_cumulative(metric, measure_ref, definitions, agg_time_dimensions or {})
        elif metric_type in ("simple", "ratio", "derived"):
            self._extract_composed(metric, measure_ref, definitions, agg_time_dimensions or {})
        elif metric_type == "conversion":
            self._extract_conversion(metric, definitions, agg_time_dimensions or {})
        else:
            logger.warning("unknown dbt metric type %r for metric %r; skipping", metric_type, name)

    def _extract_composed(
        self,
        metric: dict[str, Any],
        measure_ref: str,
        definitions: list[DefinitionEvidence],
        agg_time_dimensions: dict[str, str],
    ) -> None:
        """Propose a ``single``, ``ratio`` or ``derived`` binding for a MetricFlow metric.

        A ``simple`` metric is its measure. A ``ratio`` divides two metrics. A ``derived`` metric
        keeps MetricFlow's ``expr`` and inputs, and an input with ``offset_window`` becomes an
        ``offset`` metric named ``<input>_offset_<count>_<unit>`` that the derived metric reads.
        A metric whose ``filter``, ``offset_to_grain`` or expression cannot be carried over is
        skipped, rather than proposed as a binding that would return a different number.
        """
        name: str = metric.get("name", "")
        metric_type: str = metric.get("type", "")
        type_params: dict[str, Any] = metric.get("type_params", {})
        candidates: list[tuple[str, ContractCandidate]] = []
        reason: str | None = None
        if metric.get("filter"):
            reason = "its filter cannot be carried over"
        elif metric_type == "simple":
            measure = type_params.get("measure")
            if isinstance(measure, dict) and measure.get("filter"):
                reason = "its measure filter cannot be carried over"
            elif not measure_ref:
                reason = "it names no measure"
            else:
                candidates.append(
                    (name, ContractCandidate(kind=CandidateKind.SINGLE, measures=[measure_ref]))
                )
        elif metric_type == "ratio":
            parts = [_metric_input(type_params.get(p)) for p in ("numerator", "denominator")]
            if any(p is None or p.get("filter") for p in parts):
                reason = "a ratio part has a filter or names no metric"
            else:
                candidates.append(
                    (
                        name,
                        ContractCandidate(
                            kind=CandidateKind.RATIO,
                            measures=[],
                            metrics=[str(p["name"]) for p in parts if p is not None],
                        ),
                    )
                )
        else:
            reason, candidates = self._derived_candidates(name, type_params, agg_time_dimensions)
        if reason is not None:
            logger.info("skipping %s dbt metric %r: %s", metric_type, name, reason)
            return
        for entity, candidate in candidates:
            definitions.append(
                DefinitionEvidence(
                    source=self._source,
                    entity=entity,
                    entity_type=DefinitionEntityType.METRIC,
                    description=(metric.get("description") or None) if entity == name else None,
                    contract_candidate=candidate,
                    native_ref=metric.get("unique_id", ""),
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=_definition_fingerprint(
                        {
                            "entity": entity,
                            "entity_type": "metric",
                            "candidate": candidate.model_dump(mode="json"),
                        }
                    ),
                )
            )

    def _derived_candidates(
        self, name: str, type_params: dict[str, Any], agg_time_dimensions: dict[str, str]
    ) -> tuple[str | None, list[tuple[str, ContractCandidate]]]:
        expr = str(type_params.get("expr") or "")
        raw_inputs = [_metric_input(m) for m in type_params.get("metrics") or []]
        if not expr or not raw_inputs or any(m is None for m in raw_inputs):
            return "it names no expression or inputs", []
        offsets: list[tuple[str, ContractCandidate]] = []
        inputs: list[CandidateInput] = []
        for raw in raw_inputs:
            assert raw is not None  # noqa: S101 — checked above
            input_name = str(raw["name"])
            alias = raw.get("alias") or None
            if raw.get("filter"):
                return f"input {input_name!r} has a filter", []
            if raw.get("offset_to_grain"):
                return f"input {input_name!r} uses offset_to_grain", []
            window = _metricflow_window(raw.get("offset_window"))
            if window is None:
                inputs.append(CandidateInput(metric=input_name, alias=alias))
                continue
            if not _is_day_or_longer(window):
                return f"input {input_name!r} has an offset_window shorter than a day", []
            measure = self._metric_measures.get(input_name)
            time_dimension = agg_time_dimensions.get(measure or "")
            if time_dimension is None:
                return f"offset input {input_name!r} has no agg_time_dimension to shift", []
            duration = Duration.parse(window)
            offset_name = f"{input_name}_offset_{duration.count}_{duration.unit.value}"
            offsets.append(
                (
                    offset_name,
                    ContractCandidate(
                        kind=CandidateKind.OFFSET,
                        measures=[],
                        base=input_name,
                        time_dimension=time_dimension,
                        offset=str(duration),
                    ),
                )
            )
            inputs.append(CandidateInput(metric=offset_name, alias=alias or input_name))
        try:
            used = derived_input_names(expr)
        except ValueError as exc:
            return f"its expression cannot be carried over: {exc}", []
        names = [i.alias or i.metric for i in inputs]
        if sorted(set(used)) != sorted(set(names)):
            return "its expression does not read exactly its inputs", []
        return None, [
            *offsets,
            (
                name,
                ContractCandidate(
                    kind=CandidateKind.DERIVED, measures=[], expr=expr, inputs=inputs
                ),
            ),
        ]

    def _extract_conversion(
        self,
        metric: dict[str, Any],
        definitions: list[DefinitionEvidence],
        agg_time_dimensions: dict[str, str],
    ) -> None:
        """Propose a ``conversion`` binding for a MetricFlow conversion metric.

        The entity becomes the column each measure's semantic model declares for it, and each
        time the measure's ``agg_time_dimension``. A metric or measure ``filter`` cannot be
        carried over, and an entity missing on one side has no column to match on, so such a
        metric is skipped rather than proposed as a binding that would return another number.
        """
        name: str = metric.get("name", "")
        type_params: dict[str, Any] = metric.get("type_params", {})
        params: dict[str, Any] = type_params.get("conversion_type_params") or {}
        measures = [params.get("base_measure"), params.get("conversion_measure")]
        names = [_measure_name({"measure": m}) for m in measures]
        entity = str(params.get("entity") or "")
        calculation = str(params.get("calculation") or "conversion_rate").lower()
        window = _metricflow_window(params.get("window"))
        columns = [self._measure_entities.get(m, {}).get(entity) for m in names]
        reason: str | None = None
        if metric.get("filter") or any(isinstance(m, dict) and m.get("filter") for m in measures):
            reason = "its filter cannot be carried over"
        elif not all(names):
            reason = "it names no base or conversion measure"
        elif not entity or None in columns:
            reason = f"entity {entity!r} is not declared on the semantic model of both measures"
        elif any(m not in agg_time_dimensions for m in names):
            reason = "a measure has no agg_time_dimension"
        elif calculation not in {"conversion_rate", "conversions"}:
            reason = f"its calculation {calculation!r} is unknown"
        elif window is not None and not _is_duration(window):
            reason = f"its window {window!r} is not a duration"
        if reason is not None:
            logger.info("skipping conversion dbt metric %r: %s", name, reason)
            return
        candidate = ContractCandidate(
            kind=CandidateKind.CONVERSION,
            measures=names,
            entity={"base": str(columns[0]), "conversion": str(columns[1])},
            base_time=agg_time_dimensions[names[0]],
            conversion_time=agg_time_dimensions[names[1]],
            window=str(Duration.parse(window)) if window is not None else None,
            calculation=calculation,
            constant_properties=[
                {
                    "base": str(p.get("base_property", "")),
                    "conversion": str(p.get("conversion_property", "")),
                }
                for p in params.get("constant_properties") or []
            ],
        )
        definitions.append(
            DefinitionEvidence(
                source=self._source,
                entity=name,
                entity_type=DefinitionEntityType.METRIC,
                description=metric.get("description") or None,
                contract_candidate=candidate,
                native_ref=metric.get("unique_id", ""),
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=_definition_fingerprint(
                    {
                        "entity": name,
                        "entity_type": "metric",
                        "candidate": candidate.model_dump(mode="json"),
                    }
                ),
            )
        )

    def _extract_cumulative(
        self,
        metric: dict[str, Any],
        measure_ref: str,
        definitions: list[DefinitionEvidence],
        agg_time_dimensions: dict[str, str],
    ) -> None:
        """Propose a ``cumulative`` binding for a MetricFlow cumulative metric.

        A ``window`` becomes a trailing window and ``grain_to_date`` a period-to-date total.
        ``period_agg`` is always written, as ``first`` when MetricFlow leaves it unset, which is
        MetricFlow's own default. A metric or measure ``filter`` is Jinja over MetricFlow
        dimensions and cannot be carried over, so such a metric is skipped rather than proposed
        as a binding that would return a different number.
        """
        name: str = metric.get("name", "")
        type_params: dict[str, Any] = metric.get("type_params", {})
        nested = type_params.get("cumulative_type_params") or {}
        window = _metricflow_window(nested.get("window") or type_params.get("window"))
        grain_to_date = nested.get("grain_to_date") or type_params.get("grain_to_date")
        period_agg = str(nested.get("period_agg") or "first").lower()
        measure = type_params.get("measure")
        measure_filter = measure.get("filter") if isinstance(measure, dict) else None
        reason: str | None = None
        if window is not None and not _is_day_or_longer(window):
            reason = f"its window {window!r} is not a whole number of days or longer"
        elif grain_to_date and str(grain_to_date).lower() not in _TO_DATE_GRAINS:
            reason = f"its grain_to_date {grain_to_date!r} is not a day or longer"
        elif period_agg not in {"first", "last", "average"}:
            reason = f"its period_agg {period_agg!r} is unknown"
        elif metric.get("filter") or measure_filter:
            reason = "its filter cannot be carried over"
        elif not measure_ref:
            reason = "it names no measure"
        elif measure_ref not in agg_time_dimensions:
            reason = f"measure {measure_ref!r} has no agg_time_dimension"
        if reason is not None:
            logger.info("skipping cumulative dbt metric %r: %s", name, reason)
            return
        if window is not None:
            window = str(Duration.parse(window))
        definitions.append(
            DefinitionEvidence(
                source=self._source,
                entity=name,
                entity_type=DefinitionEntityType.METRIC,
                description=metric.get("description") or None,
                contract_candidate=ContractCandidate(
                    kind=CandidateKind.CUMULATIVE,
                    measures=[measure_ref],
                    order_by=[agg_time_dimensions[measure_ref]],
                    window=window,
                    grain_to_date=str(grain_to_date).lower() if grain_to_date else None,
                    period_agg=period_agg,
                ),
                native_ref=metric.get("unique_id", ""),
                acquisition_tier=AcquisitionTier.MODELING,
                source_fingerprint=_definition_fingerprint(
                    {
                        "entity": name,
                        "entity_type": "metric",
                        "cumulative": measure_ref,
                        **({"window": window} if window else {}),
                        **({"grain_to_date": str(grain_to_date)} if grain_to_date else {}),
                    }
                ),
            )
        )


#: The ``grain_to_date`` values a ``cumulative`` binding accepts.
_TO_DATE_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})


def _metricflow_window(raw: object) -> str | None:
    """A MetricFlow window as ``"<count> <unit>"``, from its YAML string or manifest object."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return f"{raw.get('count')} {raw.get('granularity')}"
    return str(raw)


def _is_duration(window: str) -> bool:
    try:
        Duration.parse(window)
    except ValueError:
        return False
    return True


def _is_day_or_longer(window: str) -> bool:
    try:
        duration = Duration.parse(window)
    except ValueError:
        return False
    return duration.unit.rank >= DurationUnit.DAY.rank


def _measure_name(type_params: dict[str, Any]) -> str:
    measure = type_params.get("measure")
    if isinstance(measure, dict):
        return str(measure.get("name", ""))
    return str(measure or "")


def _metric_input(raw: object) -> dict[str, Any] | None:
    """A MetricFlow metric reference as ``{"name": ..., ...}``, from its string or object form."""
    if isinstance(raw, str) and raw:
        return {"name": raw}
    if isinstance(raw, dict) and raw.get("name"):
        return raw
    return None
