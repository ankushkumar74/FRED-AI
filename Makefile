.PHONY: dev down migrate seed test eval demo lint fmt arch api worker web

dev:            ## full local stack
	docker compose up --build

down:
	docker compose down -v

migrate:
	uv run alembic -c shared/db/migrations/alembic.ini upgrade head

seed:           ## deterministic fixture with a planted root cause
	uv run python scripts/seed_demo.py --out demo.duckdb

lint:
	uv run ruff check . && uv run mypy --strict services shared agents

fmt:
	uv run ruff format .

arch:           ## module boundary contracts — see BLUEPRINT 19.3
	uv run lint-imports

test:
	uv run pytest tests/unit tests/integration -q --cov=services --cov=shared

eval: seed      ## golden-set evaluation — gates every agents/ or semantic/ change
	uv run python -m agents.evals.run --dataset tests/evals/golden.jsonl --database demo.duckdb

demo:           ## ask a question end to end, no services required
	uv run python scripts/ask.py "why did revenue drop last week"


api:
	uv run uvicorn apps.api.main:app --reload --port 8000

worker:
	uv run python -m workers.worker

web:
	cd apps/web && pnpm dev
