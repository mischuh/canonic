#!/usr/bin/env bash
# Regenerate dutch_railway.duckdb + dbt/manifest.json from scratch.
#
# Not run in CI — this is a documented recipe for rebuilding the two committed artifacts,
# e.g. to pick up a new service_date window or upstream schema change. Requires network
# access: it attaches a remote DuckDB file (blobs.duckdb.org, ~400MB, streamed not
# downloaded in full) and fetches a remote GeoJSON (cartomap.github.io).
#
# Upstream project: https://github.com/duckdb/duckdb-blog-examples/tree/main/dbt_duckdb/dutch_railway_network
# We port only its `transformation/` model layer (see examples/dutch-railway/README.md for why).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "$HERE")"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> Building dbt-duckdb venv in $WORK_DIR"
uv venv "$WORK_DIR/.venv" --python 3.12
uv pip install --python "$WORK_DIR/.venv/bin/python" dbt-duckdb

cp -r "$HERE/dbt_project" "$WORK_DIR/dutch_railway_network"
cd "$WORK_DIR/dutch_railway_network"
mv profiles.yml "$WORK_DIR/profiles.yml"
export DBT_PROFILES_DIR="$WORK_DIR"

echo "==> dbt deps"
"$WORK_DIR/.venv/bin/dbt" deps

echo "==> dbt build (models/transformation only, service_date=2024-08-01)"
"$WORK_DIR/.venv/bin/dbt" build

echo "==> Post-processing duckdb file + manifest"
"$WORK_DIR/.venv/bin/python" "$HERE/postprocess.py" \
  "$WORK_DIR/dutch_railway_network/dutch_railway.duckdb" \
  "$WORK_DIR/dutch_railway_network/target/manifest.json"

cp "$WORK_DIR/dutch_railway_network/dutch_railway.duckdb" "$EXAMPLE_DIR/dutch_railway.duckdb"
cp "$WORK_DIR/dutch_railway_network/target/manifest.json" "$EXAMPLE_DIR/dbt/manifest.json"

echo "==> Done. Verify with:"
echo "    cd $EXAMPLE_DIR && canonic status && canonic ingest --connection railway_dbt --dry-run"
