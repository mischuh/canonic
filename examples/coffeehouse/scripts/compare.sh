#!/usr/bin/env bash
# Ask dbt MetricFlow and canonic the same questions, one after the other.
#
# Requirements:
#   canonic on PATH
#   dbt and mf on PATH: pip install "dbt-metricflow[dbt-duckdb]"
#
# Usage:
#   cd examples/coffeehouse
#   bash scripts/compare.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "$SCRIPT_DIR")"
Q1=(--start-time 2026-01-01 --end-time 2026-03-31)

for tool in canonic dbt mf; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: '$tool' not found on PATH" >&2
    echo "  canonic: activate the canonic virtualenv or pip install canonic" >&2
    echo "  dbt, mf: pip install \"dbt-metricflow[dbt-duckdb]\"" >&2
    exit 1
  fi
done

ERR_LOG="$(mktemp)"
trap 'rm -f "$ERR_LOG"' EXIT

# Run a command quietly, but show its stderr if it fails.
run() {
  if ! "$@" 2>"$ERR_LOG"; then
    echo "error: '$*' failed:" >&2
    cat "$ERR_LOG" >&2
    exit 1
  fi
}

cd "$EXAMPLE_DIR/dbt"
export DBT_PROFILES_DIR=.
run dbt build --quiet

ask() {
  local title="$1" dbt_metric="$2" canonic_metric="$3"
  shift 3
  echo
  echo "### $title"
  echo "--- dbt MetricFlow"
  (cd "$EXAMPLE_DIR/dbt" && run mf query --metrics "$dbt_metric" "$@") | grep -v 'Initiating query'
  echo "--- canonic"
  local canonic_args=(--metrics "$canonic_metric")
  if [[ " $* " == *" metric_time__month "* ]]; then
    canonic_args+=(--dimensions order_month)
  else
    canonic_args+=(--filter "order_date:>=:2026-01-01" --filter "order_date:<:2026-04-01")
  fi
  (cd "$EXAMPLE_DIR" && run canonic query "${canonic_args[@]}")
}

ask "Average order value per month" average_order_value average_order_value \
  --group-by metric_time__month --order metric_time__month
ask "Average order value, Q1 2026" average_order_value average_order_value "${Q1[@]}"
ask "Active customers per month" active_customers active_customers \
  --group-by metric_time__month --order metric_time__month
ask "Active customers, Q1 2026" active_customers active_customers "${Q1[@]}"
ask "Revenue per customer, Q1 2026" revenue_per_customer revenue_per_customer "${Q1[@]}"
ask "Units sold, Q1 2026 (a metric added later)" units_sold units_sold "${Q1[@]}"
