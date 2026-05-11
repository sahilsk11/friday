#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/.friday/logs"
RUNS_DIR="${ROOT_DIR}/.friday/runs"
RUN_ID="${FRIDAY_RUN_ID:-$(date -u +"%Y%m%dT%H%M%SZ")-$$}"
RUN_DIR="${RUNS_DIR}/${RUN_ID}"
OPENCODE_BIN="${OPENCODE_BIN:-${HOME}/.opencode/bin/opencode}"
OPENCODE_HOST="${OPENCODE_HOST:-127.0.0.1}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
API_HOST="${FRIDAY_API_HOST:-0.0.0.0}"
API_PORT="${FRIDAY_API_PORT:-8000}"
WEB_PORT="${PORT:-5173}"

mkdir -p "${LOG_DIR}" "${RUN_DIR}"

replace_symlink() {
  local target="$1"
  local link_path="$2"

  rm -f "${link_path}"
  ln -s "${target}" "${link_path}"
}

replace_symlink "${RUN_DIR}" "${RUNS_DIR}/current"
replace_symlink "${RUN_DIR}" "${LOG_DIR}/current"

write_run_metadata() {
  local metadata_path="${RUN_DIR}/run.json"

  printf '{\n' >"${metadata_path}"
  printf '  "run_id": "%s",\n' "${RUN_ID}" >>"${metadata_path}"
  printf '  "started_at": "%s",\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"${metadata_path}"
  printf '  "root_dir": "%s",\n' "${ROOT_DIR}" >>"${metadata_path}"
  printf '  "opencode": {"host": "%s", "port": %s, "log": "%s"},\n' \
    "${OPENCODE_HOST}" "${OPENCODE_PORT}" "${RUN_DIR}/opencode.log" >>"${metadata_path}"
  printf '  "api": {"host": "%s", "port": %s, "log": "%s"},\n' \
    "${API_HOST}" "${API_PORT}" "${RUN_DIR}/api.log" >>"${metadata_path}"
  printf '  "agent": {"log": "%s"},\n' "${RUN_DIR}/agent.log" >>"${metadata_path}"
  printf '  "web": {"port": %s, "log": "%s"}\n' \
    "${WEB_PORT}" "${RUN_DIR}/web.log" >>"${metadata_path}"
  printf '}\n' >>"${metadata_path}"
}

link_current_file() {
  local name="$1"
  local target="${RUN_DIR}/${name}"

  : >"${target}"
  replace_symlink "${target}" "${LOG_DIR}/${name}"
}

write_run_metadata
link_current_file "opencode.log"
link_current_file "api.log"
link_current_file "agent.log"
link_current_file "web.log"
replace_symlink "${RUN_DIR}/opencode.pid" "${LOG_DIR}/opencode.pid"
replace_symlink "${RUN_DIR}/api.pid" "${LOG_DIR}/api.pid"
replace_symlink "${RUN_DIR}/agent.pid" "${LOG_DIR}/agent.pid"

wait_http() {
  local name="$1"
  local url="$2"
  local log_path="$3"
  local tries="${4:-40}"

  for _ in $(seq 1 "${tries}"); do
    if curl -fsS --max-time 2 "${url}" >/dev/null 2>&1; then
      echo "${name} is ready."
      return 0
    fi
    sleep 0.5
  done

  echo "${name} did not become ready at ${url}." >&2
  echo "Last log lines from ${log_path}:" >&2
  tail -n 80 "${log_path}" >&2 || true
  return 1
}

start_background() {
  local name="$1"
  local pid_path="$2"
  local log_path="$3"
  shift 3

  echo "Starting ${name}..."
  (
    cd "${ROOT_DIR}"
    exec "$@"
  ) >"${log_path}" 2>&1 &
  echo "$!" >"${pid_path}"
}

if [[ ! -x "${OPENCODE_BIN}" ]]; then
  echo "OpenCode binary not found or not executable: ${OPENCODE_BIN}" >&2
  echo "Set OPENCODE_BIN=/path/to/opencode and rerun make refresh." >&2
  exit 1
fi

start_background \
  "OpenCode provider" \
  "${RUN_DIR}/opencode.pid" \
  "${RUN_DIR}/opencode.log" \
  "${OPENCODE_BIN}" serve --hostname "${OPENCODE_HOST}" --port "${OPENCODE_PORT}"
wait_http \
  "OpenCode provider" \
  "http://${OPENCODE_HOST}:${OPENCODE_PORT}/config/providers" \
  "${RUN_DIR}/opencode.log"

start_background \
  "FastAPI backend" \
  "${RUN_DIR}/api.pid" \
  "${RUN_DIR}/api.log" \
  uv run uvicorn server.app.main:app --host "${API_HOST}" --port "${API_PORT}"
wait_http \
  "FastAPI backend" \
  "http://127.0.0.1:${API_PORT}/healthz" \
  "${RUN_DIR}/api.log"

start_background \
  "LiveKit agent" \
  "${RUN_DIR}/agent.pid" \
  "${RUN_DIR}/agent.log" \
  uv run python -m agent.main dev

echo "Logs:"
echo "  Run:      ${RUN_DIR}"
echo "  Current:  ${LOG_DIR}/current"
echo "  OpenCode: ${RUN_DIR}/opencode.log"
echo "  API:      ${RUN_DIR}/api.log"
echo "  Agent:    ${RUN_DIR}/agent.log"
echo "  Web:      ${RUN_DIR}/web.log"
echo
echo "Starting frontend dev server on http://localhost:${WEB_PORT} ..."
cd "${ROOT_DIR}/web"
exec > >(tee -a "${RUN_DIR}/web.log") 2>&1
exec npm run dev
