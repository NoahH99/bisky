.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help install hooks fmt lint typecheck test test-integration check run migrate \
	revision up up-core down logs metrics dashboards docker-build

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the virtualenv from uv.lock
	uv sync

hooks: ## Install the git pre-commit hooks
	uv run pre-commit install

fmt: ## Format and autofix
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Run mypy
	uv run mypy

test: ## Run the test suite
	uv run pytest

test-vv: ## Run the test suite in verbose mode
	uv run pytest -s -vv

test-integration: ## Run tests that need a real Postgres (compose must be up)
	@# A dedicated database: the integration fixtures drop and recreate the
	@# schema, which would wipe your development data.
	@docker compose exec -T postgres createdb -U bisky bisky_test 2>/dev/null || true
	BISKY_TEST_DATABASE_URL=postgresql+asyncpg://bisky:bisky@localhost:5432/bisky_test \
		uv run pytest -m integration --no-cov

check: lint typecheck test ## Everything CI runs

run: ## Run the bot locally
	uv run bisky

migrate: ## Apply migrations
	uv run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	uv run alembic revision --autogenerate -m "$(m)"

up: ## Start everything: postgres, bot, prometheus, grafana
	docker compose up -d --build

up-core: ## Start just postgres and the bot
	docker compose up -d --build bot

down: ## Stop Docker services
	docker compose down

logs: ## Tail bot logs
	docker compose logs -f bot

metrics: ## Print the bot's current metrics
	curl -fsS http://127.0.0.1:8080/metrics

dashboards: ## Open Grafana
	@echo "Grafana:    http://127.0.0.1:3000/d/bisky-overview"
	@echo "Prometheus: http://127.0.0.1:9090/targets"

docker-build: ## Build the image
	docker build -t bisky:local .
