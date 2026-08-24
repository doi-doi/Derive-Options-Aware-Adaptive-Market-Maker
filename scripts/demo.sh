#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="${CONDOR_DATA_DIR:-/Users/wilfred/Documents/Hummingbot/condor/data}"
report_dir="${STAGE65_REPORT_DIR:-${project_root}/reports/stage6_5}"

required_files=(
  "${data_dir}/derive_market_snapshots.jsonl"
  "${data_dir}/derive_market_states.jsonl"
  "${data_dir}/derive_grid_modes.jsonl"
  "${data_dir}/derive_grid_plans.jsonl"
  "${report_dir}/audit_summary.json"
)

for required_file in "${required_files[@]}"; do
  if [[ ! -s "${required_file}" ]]; then
    echo "demo input missing or empty: ${required_file}" >&2
    exit 1
  fi
done

exec python3 "${project_root}/tools/demo_status.py" \
  --data-dir "${data_dir}" \
  --report-dir "${report_dir}" \
  --config "${project_root}/integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid_testnet.example.yml"
