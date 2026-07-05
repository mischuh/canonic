"""Post-build fixups for the Dutch Railway Network example — run by build.sh, not standalone.

Two things `dbt build` cannot do on its own:

1. The `.duckdb` file still has `GEOMETRY` columns (spatial extension) and a
   `TIMESTAMP WITH TIME ZONE` column. Canonic has no geometry type and DuckDB's Python
   driver needs `pytz` to read timestamptz — neither is a Canonic dependency, so both are
   converted to plain types the DuckDB connector reads natively, with no extension loaded.
2. dbt-duckdb's compiled manifest carries `database: <catalog>` on every model, but the
   Canonic DuckDB connector introspects relations as `main.<table>` (no catalog prefix) —
   see canonic/connectors/duckdb.py. Left alone, the dbt connector's evidence would never
   match the live schema during reconciliation. It also has no `data_type`/constraint info
   (dbt only fills those in from an explicit `constraints:` block, which this project's
   schema.yml doesn't declare) — so this fills in both from what we know the build produces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

DUCKDB_PATH = Path(sys.argv[1])
MANIFEST_PATH = Path(sys.argv[2])

# (model, column) -> (new_column_name | None, data_type)
_GEOMETRY_COLUMNS = {
    ("dim_nl_provinces", "province_geometry"): ("province_centroid_wkt", "varchar"),
    ("dim_nl_municipalities", "municipality_geometry"): ("municipality_centroid_wkt", "varchar"),
    ("dim_nl_train_stations", "station_geo_location"): ("station_location_wkt", "varchar"),
}

_DATA_TYPES = {
    "dim_nl_provinces": {
        "province_sk": "varchar",
        "province_id": "integer",
        "province_name": "varchar",
        "last_updated_dt": "timestamp",
        "invocation_id": "varchar",
    },
    "dim_nl_municipalities": {
        "municipality_sk": "varchar",
        "municipality_id": "integer",
        "municipality_name": "varchar",
        "province_sk": "varchar",
        "last_updated_dt": "timestamp",
        "invocation_id": "varchar",
    },
    "dim_nl_train_stations": {
        "station_sk": "varchar",
        "station_id": "bigint",
        "station_code": "varchar",
        "station_name": "varchar",
        "station_type": "varchar",
        "municipality_sk": "varchar",
        "last_updated_dt": "timestamp",
        "invocation_id": "varchar",
    },
    "fact_services": {
        "service_sk": "varchar",
        "service_date": "date",
        "service_type": "varchar",
        "service_company": "varchar",
        "station_sk": "varchar",
        "station_arrival_time": "timestamp",
        "station_departure_time": "timestamp",
        "service_arrival_cancelled": "boolean",
        "service_train_number": "bigint",
        "service_departure_cancelled": "boolean",
        "last_updated_dt": "timestamp",
        "invocation_id": "varchar",
    },
    "ams_traffic_v": {"service_sk": "varchar", "station_service_time": "timestamp"},
}

# model -> {column: "primary_key"} | {column: (relation, ref_column)}
_CONSTRAINTS = {
    "dim_nl_provinces": {"province_sk": "pk"},
    "dim_nl_municipalities": {
        "municipality_sk": "pk",
        "province_sk": ("main.dim_nl_provinces", "province_sk"),
    },
    "dim_nl_train_stations": {
        "station_sk": "pk",
        "municipality_sk": ("main.dim_nl_municipalities", "municipality_sk"),
    },
    "fact_services": {
        "service_sk": "pk",
        "station_sk": ("main.dim_nl_train_stations", "station_sk"),
    },
}


def fix_duckdb_file() -> None:
    con = duckdb.connect(str(DUCKDB_PATH))
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")

    for (model, column), (new_name, _dtype) in _GEOMETRY_COLUMNS.items():
        # Centroid, not the full boundary — a province polygon's WKT is tens of KB.
        con.execute(
            f"ALTER TABLE main.{model} ALTER COLUMN {column} "
            f"TYPE VARCHAR USING ST_AsText(ST_Centroid({column}))"
            if model != "dim_nl_train_stations"
            else f"ALTER TABLE main.{model} ALTER COLUMN {column} TYPE VARCHAR USING ST_AsText({column})"
        )
        con.execute(f"ALTER TABLE main.{model} RENAME COLUMN {column} TO {new_name}")

    for model in _DATA_TYPES:
        if "last_updated_dt" in _DATA_TYPES[model]:
            con.execute(
                f"ALTER TABLE main.{model} ALTER COLUMN last_updated_dt "
                "TYPE TIMESTAMP USING last_updated_dt::TIMESTAMP"
            )

    con.execute("CHECKPOINT")
    con.close()


def fix_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    nodes = manifest["nodes"]
    renames = {
        model: {old: new for (m, old), (new, _) in _GEOMETRY_COLUMNS.items() if m == model}
        for model in _DATA_TYPES
    }

    for model, dtypes in _DATA_TYPES.items():
        node = nodes[f"model.dutch_railway_network.{model}"]
        node["database"] = None  # match DuckDBConnector's un-prefixed `main.<table>` relations

        new_columns = {}
        for old_name, spec in node["columns"].items():
            new_name = renames[model].get(old_name, old_name)
            spec["name"] = new_name
            if new_name in dtypes:
                spec["data_type"] = dtypes[new_name]
            new_columns[new_name] = spec
        node["columns"] = new_columns

        for column, constraint in _CONSTRAINTS.get(model, {}).items():
            if constraint == "pk":
                new_columns[column]["constraints"] = [{"type": "primary_key"}]
            else:
                to_relation, to_column = constraint
                new_columns[column]["constraints"] = [
                    {"type": "foreign_key", "to": to_relation, "to_columns": [to_column]}
                ]

    MANIFEST_PATH.write_text(json.dumps(manifest))


if __name__ == "__main__":
    fix_duckdb_file()
    fix_manifest()
    print(f"post-processed {DUCKDB_PATH} and {MANIFEST_PATH}")
