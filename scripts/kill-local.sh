#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/.friday/logs"
LOCAL_PORTS="${LOCAL_PORTS:-8000 5173 4096}"

collect_descendants() {
  local pid="$1"
  local child

  pgrep -P "${pid}" 2>/dev/null | while read -r child; do
    collect_descendants "${child}"
    echo "${child}"
  done
}

terminate_pids() {
  local signal="$1"
  shift || true

  local pid
  for pid in "$@"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "-${signal}" "${pid}" 2>/dev/null || true
    fi
  done
}

kill_tree() {
  local pid="$1"
  local descendants

  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    return
  fi

  descendants="$(collect_descendants "${pid}" | sort -rn || true)"
  # shellcheck disable=SC2086
  terminate_pids TERM ${descendants} "${pid}"
  sleep 0.5
  # shellcheck disable=SC2086
  terminate_pids KILL ${descendants} "${pid}"
}

kill_pid_file_tree() {
  local pid_path="$1"

  if [[ ! -f "${pid_path}" ]]; then
    return
  fi

  local pid
  pid="$(tr -dc '0-9' <"${pid_path}")"
  kill_tree "${pid}"
  rm -f "${pid_path}"
}

kill_port_listeners() {
  local signal="$1"
  local port
  local pids

  for port in ${LOCAL_PORTS}; do
    pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      # shellcheck disable=SC2086
      terminate_pids "${signal}" ${pids}
    fi
  done
}

kill_agent_log_writers() {
  local log_path="${LOG_DIR}/agent.log"
  local pids

  if [[ ! -f "${log_path}" ]]; then
    return
  fi

  pids="$(lsof -t "${log_path}" 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    # shellcheck disable=SC2086
    terminate_pids TERM ${pids}
    sleep 0.5
    # shellcheck disable=SC2086
    terminate_pids KILL ${pids}
  fi
}

kill_orphaned_livekit_workers() {
  local pid
  local command

  while read -r pid command; do
    if [[ "${command}" != *"multiprocessing."* ]]; then
      continue
    fi

    if lsof -p "${pid}" 2>/dev/null | grep -Fq "${ROOT_DIR}/.venv/lib/python"; then
      terminate_pids TERM "${pid}"
    fi
  done < <(ps -Ao pid=,command=)

  sleep 0.5

  while read -r pid command; do
    if [[ "${command}" != *"multiprocessing."* ]]; then
      continue
    fi

    if lsof -p "${pid}" 2>/dev/null | grep -Fq "${ROOT_DIR}/.venv/lib/python"; then
      terminate_pids KILL "${pid}"
    fi
  done < <(ps -Ao pid=,command=)
}

echo "Stopping local dev processes (keeping Docker services running)..."
mkdir -p "${LOG_DIR}"

kill_pid_file_tree "${LOG_DIR}/agent.pid"
kill_pid_file_tree "${LOG_DIR}/api.pid"
kill_pid_file_tree "${LOG_DIR}/opencode.pid"

kill_port_listeners TERM
pkill -f "python -m agent.main dev" 2>/dev/null || true
pkill -f "opencode serve" 2>/dev/null || true
kill_agent_log_writers
kill_orphaned_livekit_workers
kill_port_listeners KILL

echo "Done."
