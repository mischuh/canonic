#!/usr/bin/env bash
# Regenerate jaffle_shop.duckdb and dbt/manifest.json from the upstream project.
#
# Requirements:
#   pip install dbt-core dbt-duckdb
#
# Usage:
#   cd examples/jaffle-shop
#   bash scripts/build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "$SCRIPT_DIR")"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Cloning jaffle-shop..."
git clone --depth=1 https://github.com/dbt-labs/jaffle-shop "$WORK_DIR/jaffle-shop"

echo "Running dbt build (DuckDB)..."
cd "$WORK_DIR/jaffle-shop"
# dbt-duckdb writes to the path in profiles.yml; override to our target
dbt build --profiles-dir . --target dev 2>&1

echo "Copying artifacts..."
cp "$WORK_DIR/jaffle-shop/jaffle_shop.duckdb" "$EXAMPLE_DIR/jaffle_shop.duckdb"
cp "$WORK_DIR/jaffle-shop/target/manifest.json" "$EXAMPLE_DIR/dbt/manifest.json"

echo "Done. Database: $EXAMPLE_DIR/jaffle_shop.duckdb"
echo "      Manifest: $EXAMPLE_DIR/dbt/manifest.json"
