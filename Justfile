# Run `just` to see recipes. `just check` is what CI runs.

default:
    @just --list

# Format, lint, type-check and test (offline)
check: fmt-check lint typecheck test

# Apply formatting and safe lint fixes
fix:
    uv run ruff format .
    uv run ruff check --fix .

fmt-check:
    uv run ruff format --check .

lint:
    uv run ruff check .

typecheck:
    uv run pyright

test *args:
    uv run pytest --cov --cov-report=term {{args}}

# Tests that use the real internet and your browser cookies
test-live *args:
    uv run pytest -m live -v {{args}}
