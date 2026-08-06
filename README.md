# AI Analyst

Production skeleton for the AI Analyst platform. The full architectural rationale
lives in [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) — read it before changing structure.

## Quick start

```bash
cp .env.example .env
make dev          # docker compose: api, worker, web, postgres, redis, minio
make migrate      # alembic upgrade head
make seed         # synthetic demo org with planted ground truth
make test         # unit + integration
make eval         # AI golden-set evaluation
```

## The three invariants

1. **The LLM never emits a number.** Numbers come from executed queries, become
   `Finding` rows, and are cited by `finding_id` in every narrative.
2. **`services/` is framework-free and `services/evidence/` is LLM-free.**
   Both are enforced by `import-linter` in CI, not by discipline.
3. **Every tenant table carries `org_id` with a Postgres RLS policy.**
   Application-level filtering is never the isolation mechanism.

## Layout

| Path | Contains |
|---|---|
| `apps/web` | Next.js frontend |
| `apps/api` | FastAPI HTTP layer — routing and serialization only, no business logic |
| `services/` | Business logic. No web framework imports. Callable from API, workers, Slack |
| `agents/` | Non-deterministic LLM code. May call services; services may never call agents |
| `workers/` | Async entry points; logic lives in `services/` |
| `pipelines/` | Ingestion, transformation, data quality |
| `shared/` | DB models, protocols, events, telemetry. Imports nothing internal |
| `infra/` | Terraform, Docker, k8s manifests |
| `tests/` | Unit, integration, e2e, **evals** |
| `configs/` | Settings, starter metric packs, prompt pinning, flags |
| `docs/adr/` | One ADR per hard decision. See BLUEPRINT Appendix A |

## Phase 1 scope

Postgres + CSV + Stripe connectors · semantic layer v1 · lookup and comparison
intents · SQL trace UI · eval harness. See BLUEPRINT Part 20.2.
