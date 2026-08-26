#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting local Derive Adaptive State Grid dashboard at http://localhost:8501"
exec streamlit run "${REPO_ROOT}/dashboard/app.py" -- "$@"
