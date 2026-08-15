#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${SCRIPT_DIR}/venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "錯誤：找不到 ${PYTHON_BIN}，請先建立虛擬環境。" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/review_site.py" "$@"
