.PHONY: install verify site dev down migrate seed test eval demo lint fmt arch api worker web

install:        ## editable install with dev + docs extras
	pip install -e ".[dev,docs]"

verify:         ## everything CI runs, in CI's order — run this before pushing
	@echo "note: CI uses Python 3.12. Verifying on another version can miss"
	@echo "      type errors that only appear with 3.12's dependency resolution."
	@python -c "import sys; print(f'  running on {sys.version.split()[0]}')"
	ruff check --output-format=concise .
	mypy --strict services shared agents
	lint-imports --config .importlinter
	pytest tests/unit tests/integration \
	  --cov=services/semantic --cov=services/query --cov=services/evidence \
	  --cov-fail-under=85
	python scripts/seed_demo.py --out demo.duckdb
	python -m agents.evals.run --dataset tests/evals/golden.jsonl --database demo.duckdb
	python scripts/build_docs.py && cp mkdocs.yml /tmp/mkdocs.bak && cat nav.generated.yml >> mkdocs.yml \
	  && mkdocs build --strict; cp /tmp/mkdocs.bak mkdocs.yml
	@echo "all CI checks passed"

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
	python scripts/ask.py "why did revenue drop last week"

site:           ## build the docs site + browser demo, then serve it locally
	python scripts/build_docs.py
	python scripts/build_demo.py --seed
	cp mkdocs.yml /tmp/mkdocs.bak && cat nav.generated.yml >> mkdocs.yml \
	  && mkdocs build --strict; cp /tmp/mkdocs.bak mkdocs.yml
	@echo "serving at http://localhost:8000 — the demo is at /demo/"
	cd site && python -m http.server 8000


api:
	uv run uvicorn apps.api.main:app --reload --port 8000

worker:
	uv run python -m workers.worker

web:
	cd apps/web && pnpm dev
