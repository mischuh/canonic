"""dbt manifest connector — parse compiled manifest.json → normalized evidence (SPEC-E3 §4, S1).

Emits :class:`RelationSchema` at acquisition tier ``modeling`` (ladder tier 2) and
:class:`DefinitionEvidence` for measures, dimensions, joins, and entities.  No dbt-specific
structure crosses the boundary into E4; ``native_ref`` carries the dbt ``unique_id`` as the
sole back-pointer for provenance (SPEC-E3 §3.1).

Version handling: the manifest schema version and dbt version are detected and logged at
``test_connection`` time but never block extraction in v1 (hard out-of-range rejection is S5).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    ForeignKey,
    ForeignKeyRef,
    Health,
    JoinSpec,
    RelationSchema,
    compute_fingerprint,
)
from canon.semantic.models import Additivity, Relationship

logger = logging.getLogger(__name__)

__all__ = ["DbtConnector"]

# dbt aggregate function name → Canon Additivity.  All others are recorded
# with a warning and omitted (additivity=None → "unknown" per SPEC-E3 §3.1).
_ADDITIVE_AGGS: frozenset[str] = frozenset({"sum", "count", "min", "max", "count_distinct"})
_NON_ADDITIVE_AGGS: frozenset[str] = frozenset(
    {"average", "median", "percentile", "percentile_cont", "percentile_disc"}
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


class DbtConnector:
    """Definition connector for a compiled dbt ``manifest.json`` (SPEC-E3 §4, dbt Core 1.6+).

    Constructed directly from a manifest path (no live database connection).
    Version detection is performed at ``test_connection`` time; out-of-range rejection
    is deferred to a future S5 PR.

    Args:
        manifest_path: Path to the compiled ``manifest.json``.
        source: Connection id used to stamp evidence items (default ``"dbt"``).
    """

    def __init__(self, manifest_path: str | Path, *, source: str = "dbt") -> None:
        self._path = Path(manifest_path)
        self._source = source

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_DEFINITIONS]

    async def test_connection(self) -> Health:
        """Verify the manifest exists, parses as JSON, and carry version metadata."""
        if not self._path.exists():
            return Health(status="error", message=f"manifest not found: {self._path}")
        try:
            manifest = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            return Health(status="error", message=f"cannot read manifest: {exc}")
        metadata = manifest.get("metadata", {})
        dbt_version = metadata.get("dbt_version", "unknown")
        schema_version = _parse_manifest_version(metadata.get("dbt_schema_version")) or "unknown"
        return Health(status="ok", message=f"dbt {dbt_version}, manifest {schema_version}")

    async def extract_definitions(self) -> DefinitionExtract:
        """Parse the manifest and return normalized schemas + definition evidence."""
        manifest = json.loads(self._path.read_text())
        nodes: dict[str, Any] = manifest.get("nodes", {})
        raw_sm = manifest.get("semantic_models", {})
        semantic_models: list[dict[str, Any]] = list(raw_sm.values()) if isinstance(raw_sm, dict) else list(raw_sm)
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
        for metric in metrics.values():
            self._extract_metric(metric, definitions)

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

            expr = measure.get("expr") or m_name
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
                    native_ref=f"{unique_id}#{m_name}",
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
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
        self, metric: dict[str, Any], definitions: list[DefinitionEvidence]
    ) -> None:
        """Emit a MEASURE DefinitionEvidence for one dbt metric."""
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

        if metric_type in ("simple", "additive", "sum", "count", "average", "min", "max"):
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
                    native_ref=unique_id,
                    acquisition_tier=AcquisitionTier.MODELING,
                    source_fingerprint=fp,
                )
            )
        else:
            logger.warning(
                "unsupported dbt metric type %r for metric %r; skipping", metric_type, name
            )
