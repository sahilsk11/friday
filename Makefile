.PHONY: setup livekit api api-reload agent opencode web dev web-deps self-test text-test kill kill-local down nuke refresh logs

LOCAL_PORTS := 8000 5173 4096
OPENCODE_BIN ?= $(HOME)/.opencode/bin/opencode
OPENCODE_HOST ?= 127.0.0.1
OPENCODE_PORT ?= 4096

setup:
	cp -n .env.example .env 2>/dev/null || true
	uv sync
	cd web && npm install

livekit:
	docker compose up -d livekit

api:
	uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000

api-reload:
	uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir server --reload-dir agent

agent:
	uv run python -m agent.main dev

opencode:
	$(OPENCODE_BIN) serve --hostname $(OPENCODE_HOST) --port $(OPENCODE_PORT)

web-deps:
	cd web && npm install

web:
	cd web && npm run dev

self-test:
	cd web && npm run friday:self-test -- --headless

text-test:
	cd web && npm run friday:text-test -- --headless

kill: kill-local

kill-local:
	@LOCAL_PORTS="$(LOCAL_PORTS)" bash scripts/kill-local.sh

down:
	docker compose down 2>/dev/null || true

nuke: kill-local down

refresh: kill-local livekit
	@bash scripts/refresh-local.sh

logs:
	@bash scripts/show-current-logs.sh

dev: livekit api agent web
