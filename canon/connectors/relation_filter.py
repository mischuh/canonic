"""Schema/table narrowing for connector introspection (setup-time warehouse scoping).

Applied by connectors right after they enumerate relations, so every consumer of
``introspect_schema()`` — the CLI preview, the full ingest pipeline, and the acquisition
ladder — sees the same narrowed set.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def filter_relations(
    relations: dict[tuple[str, str], str],
    schemas: list[str] | None,
    tables: list[str] | None,
) -> dict[tuple[str, str], str]:
    """Narrow relations to the given schemas and/or table patterns.

    ``schemas`` matches schema names exactly; ``None``/empty means no schema filtering.
    ``tables`` entries are glob patterns (fnmatch, case-sensitive) matched against both
    the fully-qualified "schema.table" name and the bare table name, so both
    "public.fact_*" and bare "fact_*" work; ``None``/empty means no table filtering.
    Schema filtering is applied before table filtering, so a table pattern can never
    resurrect a relation from an excluded schema.
    """
    result = relations
    if schemas:
        allowed_schemas = set(schemas)
        result = {key: kind for key, kind in result.items() if key[0] in allowed_schemas}
    if tables:
        result = {
            key: kind for key, kind in result.items() if _matches_any_table_pattern(key, tables)
        }
    return result


def _matches_any_table_pattern(relation: tuple[str, str], patterns: list[str]) -> bool:
    schema, name = relation
    qualified = f"{schema}.{name}"
    return any(
        fnmatchcase(qualified, pattern) or fnmatchcase(name, pattern) for pattern in patterns
    )
