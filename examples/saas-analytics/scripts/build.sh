#!/usr/bin/env bash
# Regenerate saas.duckdb from setup.sql.
#
# Requirements:
#   duckdb CLI (https://duckdb.org/docs/installation/)  — or run setup.sql
#   through any DuckDB client.
#
# Usage:
#   cd examples/saas-analytics
#   bash scripts/build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$EXAMPLE_DIR"
echo "Rebuilding saas.duckdb from setup.sql..."
rm -f saas.duckdb
# Create with a small block size — this dataset is tiny, so 16 KiB blocks keep the
# committed file compact (the 256 KiB default would bloat it ~3x). Functionally identical.
{ echo "ATTACH 'saas.duckdb' AS saas (BLOCK_SIZE 16384); USE saas;"; cat setup.sql; } | duckdb :memory:

echo "Done. Database: $EXAMPLE_DIR/saas.duckdb"
