.PHONY: dev down migrate seed test eval lint fmt arch api worker web

dev:            ## full local stack
	docker compose up --build

down:
	docker compose down -v

migrate:
	uv run alembic -c shared/db/migrations/alembic.ini upgrade head

seed:
	uv run python scripts/seed_demo.py

lint:
	uv run ruff check . && uv run mypy --strict services shared agents

fmt:
	uv run ruff format .

arch:           ## module boundary contracts — see BLUEPRINT 19.3
	uv run lint-imports

test:
	uv run pytest tests/unit tests/integration -q --cov=services --cov=shared

eval:           ## AI golden-set evaluation — gates every agents/ change
	uv run python -m agents.evals.run --dataset tests/evals/golden.jsonl

api:
	uv run uvicorn apps.api.main:app --reload --port 8000

worker:
	uv run python -m workers.worker

web:
	cd apps/web && pnpm dev
