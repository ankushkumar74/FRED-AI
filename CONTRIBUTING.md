# Contributing

## The three invariants

Changes that break any of these will be rejected regardless of how much they improve
something else. They are the product.

1. **The LLM never emits a number.** Figures come from executed queries, become
   `Finding` rows, and are cited by `finding_id` in every narrative. If you find
   yourself asking a model to compute, aggregate, or recall a value, stop.
2. **`services/evidence/` never imports a model library.** Enforced by the
   `evidence-is-llm-free` contract in `.importlinter`.
3. **Every tenant table carries `org_id` with an RLS policy.** Application-level
   filtering is not the isolation mechanism and never becomes one.

## Before opening a PR

```bash
make lint     # ruff + mypy --strict
make arch     # import-linter module boundary contracts
make test     # unit + integration, 80% floor, 95% on services/evidence
make eval     # AI golden-set gates — required for any agents/ change
```

## Prompts are code

Prompt templates live in `agents/prompts/` and are versioned in
`configs/prompts.yaml`. A prompt change is a behaviour change: it needs review and
it needs an eval run. `numeric_fidelity` is a hard 100% gate — there is no
threshold below which shipping a fabricated number is acceptable.

## Architecture decisions

Anything that changes a decision recorded in `docs/adr/` needs a new ADR
superseding the old one. Don't edit history; append to it. The reasoning matters
more than the conclusion, because in eighteen months someone will ask why.

## Editing the blueprint

`docs/BLUEPRINT.md` is the single source of truth. The published site is generated
from it by `scripts/build_docs.py` at build time — never edit `site_docs/`.
