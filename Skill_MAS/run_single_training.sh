#!/usr/bin/env bash
# Launcher for the portable single-dataset Skill-MAS repository.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "${ROOT}/.." && pwd)"
PYTHON_BIN="${SKILL_MAS_PYTHON:-python3}"
ENV_FILE="${SKILL_MAS_ENV_FILE:-}"

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || { echo "Python executable not found: ${PYTHON_BIN}" >&2; exit 1; }

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi
export PYTHONPATH="${PACKAGE_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON_BIN}" -m Skill_MAS.core.cli "$@"
