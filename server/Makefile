.PHONY: install lint lint-fix typecheck deadcode filelen check test fmt run clean

install:
	uv sync --all-extras

# ─── Quality gates ──────────────────────────────────────────────────────────

lint:
	uv run ruff check friday tests

lint-fix:
	uv run ruff check friday tests --fix
	uv run ruff format friday tests

fmt:
	uv run ruff format friday tests

typecheck:
	uv run pyright

deadcode:
	uv run vulture friday --min-confidence 80

filelen:
	@uv run python scripts/check_file_length.py friday tests

# Run everything CI would run.
check: lint typecheck deadcode filelen test

# ─── Tests ──────────────────────────────────────────────────────────────────

test:
	uv run pytest

# ─── Run ────────────────────────────────────────────────────────────────────

run:
	uv run uvicorn friday.main:app --reload --host 0.0.0.0 --port 7860

clean:
	rm -rf .venv .pytest_cache .ruff_cache **/__pycache__
