#!/usr/bin/env bash
# Regenerate coffeehouse.duckdb from setup.sql.
#
# Requirements:
#   duckdb CLI (https://duckdb.org/docs/installation/)
#
# Usage:
#   cd examples/coffeehouse
#   bash scripts/build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$EXAMPLE_DIR"
echo "Rebuilding coffeehouse.duckdb from setup.sql..."
rm -f coffeehouse.duckdb
# Small block size keeps the committed file compact. Functionally identical.
{ echo "ATTACH 'coffeehouse.duckdb' AS coffeehouse (BLOCK_SIZE 16384); USE coffeehouse;"; cat setup.sql; } | duckdb :memory:

echo "Done. Database: $EXAMPLE_DIR/coffeehouse.duckdb"
