.PHONY: help install migrate verify test lint fmt health pg-up pg-down

help:
	@echo "install  - create .venv and install dev deps (uv)"
	@echo "migrate  - apply migrations to KOSH_DATABASE_URL"
	@echo "verify   - run the six foundation checks end to end"
	@echo "test     - pytest"
	@echo "lint     - ruff check"
	@echo "health   - daily collection health report"
	@echo "pg-up    - start Postgres in docker"

install:
	uv venv
	uv pip install -e ".[dev]"

migrate:
	python -m core.cli migrate

verify:
	python -m core.cli verify-foundation

test:
	python -m pytest -q

lint:
	ruff check core sources tests
	ruff format --check core sources tests

fmt:
	ruff format core sources tests

health:
	python -m core.cli health

pg-up:
	docker compose up -d
	docker compose exec -T postgres pg_isready -U kosh -d kosh

pg-down:
	docker compose down
