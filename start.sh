#!/usr/bin/env bash
# Start local Friday backend/frontend. Stays alive; Ctrl+C kills everything.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PIDS=()

kill_port() {
  local port=$1
  local pids
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    echo "Killing processes on port $port: $pids"
    echo "$pids" | xargs kill -9 2>/dev/null || true
  fi
}

is_port_open() {
  lsof -ti tcp:"$1" &>/dev/null
}

cleanup() {
  echo ""
  echo "Shutting down..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Kill existing local Friday dev processes ──────────────────────────────────
kill_port 5173
kill_port 8000

# ── OpenCode server (port 4096) ───────────────────────────────────────────────
if is_port_open 4096; then
  echo "[opencode] Reusing existing server on :4096."
else
  echo "[opencode] Nothing listening on :4096; starting local server..."
  env -u OPENCODE_SERVER_USERNAME -u OPENCODE_SERVER_PASSWORD \
    opencode serve --hostname 127.0.0.1 --port 4096 2>&1 | sed 's/^/[opencode] /' &
  PIDS+=($!)
fi

# ── Backend / FastAPI (port 8000) ─────────────────────────────────────────────
echo "[backend]  Starting on :8000..."
(cd "$REPO/server" && uv run uvicorn friday.main:app --reload --port 8000) 2>&1 | sed 's/^/[backend]  /' &
PIDS+=($!)

# ── Frontend / Vite (port 5173) ───────────────────────────────────────────────
if [[ ! -d "$REPO/web/node_modules" ]]; then
  echo "[frontend] node_modules missing — running npm install..."
  (cd "$REPO/web" && npm install)
fi
echo "[frontend] Starting on :5173..."
(cd "$REPO/web" && npm run dev) 2>&1 | sed 's/^/[frontend] /' &
PIDS+=($!)

echo ""
echo "  OC  → http://localhost:4096 (shared)"
echo "  BE  → http://localhost:8000"
echo "  FE  → http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop all services."

wait
