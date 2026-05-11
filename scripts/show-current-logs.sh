#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/.friday/logs"
RUNS_DIR="${ROOT_DIR}/.friday/runs"
CURRENT_RUN="${RUNS_DIR}/current"
TAIL_LINES="${TAIL_LINES:-80}"

if [[ ! -e "${CURRENT_RUN}" ]]; then
  if [[ ! -d "${LOG_DIR}" ]]; then
    echo "No Friday logs found at ${CURRENT_RUN} or ${LOG_DIR}." >&2
    exit 1
  fi

  echo "No current run pointer found. Showing legacy logs from ${LOG_DIR}."
  echo
  RUN_DIR="${LOG_DIR}"
else
  RUN_DIR="$(cd "${CURRENT_RUN}" && pwd)"
fi

echo "Current Friday run: ${RUN_DIR}"
echo

if [[ -f "${RUN_DIR}/run.json" ]]; then
  echo "run.json"
  sed -n '1,120p' "${RUN_DIR}/run.json"
  echo
fi

for name in opencode api agent web; do
  log_path="${RUN_DIR}/${name}.log"
  if [[ ! -f "${log_path}" ]]; then
    continue
  fi

  echo "==> ${name}.log <=="
  tail -n "${TAIL_LINES}" "${log_path}" || true
  echo
done
