.PHONY: fmt lint lint-lazy test test-integration test-integration-llm test-e2e test-all typecheck deptry check check-layer install dev build clean

fmt:
	uv run ruff format src/ tests/ ccgram-pro/src/ ccgram-pro/tests/

lint: lint-lazy
	uv run ruff check src/ tests/ ccgram-pro/src/ ccgram-pro/tests/

lint-lazy:
	uv run python scripts/lint_lazy_imports.py
	uv run python scripts/lint_lazy_imports.py ccgram-pro/src

typecheck:
	uv run pyright src/ccgram/ tests/
	uv run pyright ccgram-pro/src/

deptry:
	uv run deptry src

test:
	uv run pytest tests/ -m "not integration and not e2e" -n auto --dist=loadscope

# ccgram-pro layer — runs alongside `check` so a layer regression fails CI.
check-layer:
	uv run ruff format --check ccgram-pro/src/ ccgram-pro/tests/
	uv run ruff check ccgram-pro/src/ ccgram-pro/tests/
	uv run python scripts/lint_lazy_imports.py ccgram-pro/src
	uv run pyright ccgram-pro/src/
	TELEGRAM_BOT_TOKEN=test ALLOWED_USERS=1 uv run pytest ccgram-pro/tests/ -q

test-serial:
	uv run pytest tests/ -m "not integration and not e2e"

test-integration:
	uv run pytest tests/integration/ -m "not llm" -n auto --dist=loadscope -v

test-integration-llm:
	uv run pytest tests/integration/ -m "llm" -v

test-e2e:
	uv run pytest tests/e2e/ -v --timeout=300

test-all:
	uv run pytest tests/ -n auto --dist=loadscope -v -m "not e2e"

check: fmt lint typecheck deptry test test-integration check-layer

install:
	uv sync

dev:
	uv sync --extra dev

build:
	uv build

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
