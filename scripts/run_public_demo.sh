#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Virtual environment not found. Run the setup commands in README.md first." >&2
  exit 1
fi

export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m derive_options_mm.public_demo "$@"
