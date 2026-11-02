# AI Analyst

Production skeleton for the AI Analyst platform. The full architectural rationale
lives in [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) — read it before changing structure.

## Quick start

No services required — the Phase 1 slice runs entirely on DuckDB:

```bash
pip install sqlglot duckdb pydantic pyarrow pyyaml pytest
python scripts/seed_demo.py --out demo.duckdb      # ~3s, deterministic
python scripts/ask.py "revenue by channel last month"
```

```
  Q: revenue by channel last month

  Gross Revenue for last month, by channel
  ----------------------------------------
  Organic                           $208,017.04
  Paid Search                       $204,928.02
  Paid Social                       $160,050.33
  Email                              $91,119.32
  Affiliate                          $64,216.09

  [1ms | 5 rows | fct_orders | metric certified]
```

Try the cases where it declines, which matter more than the ones where it answers:

```bash
python scripts/ask.py "what is our conversion rate"   # undefined metric -> offers to define
python scripts/ask.py "revenue by product category"   # fan-out join -> refuses
python scripts/ask.py "CAC by device"                 # unshared dimension -> refuses
python scripts/ask.py "why did revenue drop last week" --trace
```

The full loop:

```bash
make test    # 116 unit + integration tests
make eval    # golden-set evaluation against configs/eval_gates.yaml
make arch    # import-linter module boundary contracts
make dev     # docker compose: api, worker, web, postgres, redis, minio
```

## What works today

Phase 1 of [the roadmap](docs/BLUEPRINT.md#part-20--roadmap) — question to correct
number, with the SQL visible:

| Component | State |
|---|---|
| Semantic layer: entities, dimensions, joins, metric packs | working |
| Metric kinds: simple, derived, same-entity ratio, **cross-entity ratio** | working |
| SQLGlot compiler, 4 dialects from one definition | working |
| Guarantees: filter injection, ratio recomputation, fan-out refusal, period alignment | working |
| NL -> MetricRef resolution with clarifying questions | working, 100% on the golden set |
| AST validator: read-only, allowlist, limit injection, lineage | working |
| Executor: DuckDB + Postgres, Arrow results, result cache | working |
| Deterministic fixture with planted root cause | working |
| Eval harness with five gated scorers | working |
| Evidence engine, agents, memory, alerts | **not started** — Phase 2+ |

Current eval scores, all gated in `configs/eval_gates.yaml`:

```
clarification_recall       6/6     100%   gate  85%
dimension_accuracy        12/12    100%   gate  90%
refusal_correctness        4/4     100%   gate 100%
resolution_accuracy       56/56    100%   gate  95%
sql_execution_accuracy    18/18    100%   gate  95%
```

`sql_execution_accuracy` compares compiled SQL against **independently hand-written
reference SQL** in `tests/evals/golden.jsonl`, so a compiler bug surfaces as a value
mismatch rather than the compiler quietly agreeing with itself.

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

## Next

Phase 2 is the differentiator: the evidence engine (`services/evidence/`) — STL
seasonality, robust anomaly detection, and mix-shift decomposition scored against the
planted ground truth already in the fixture. See
[BLUEPRINT Part 12](docs/BLUEPRINT.md#part-12--root-cause-engine).

Until it exists, a "why" question returns the trend and says plainly that it cannot
explain the cause yet. That is the intended behaviour, not a gap to paper over.
