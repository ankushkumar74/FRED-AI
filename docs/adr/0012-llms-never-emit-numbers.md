# ADR 0012 — LLMs never emit numbers

**Status:** Accepted · **Date:** 2026-09-01 · **Blueprint:** Parts 1, 9, 12

## Context

The product's entire claim is that its answers are true. A language model asked
to analyze data will produce fluent, plausible, and sometimes fabricated
figures. In a product where a CFO may repeat our number to a board, a confident
wrong answer is worse than no answer.

## Options considered

1. **Dump result rows into the context window and let the model analyze.**
   Simplest, fastest to build, and the approach most competitors take. Fails on
   data volume, cost, and — decisively — numeric integrity.
2. **Model generates analysis; a second model checks it.** Cheaper than option 3
   but the checker shares the generator's failure modes.
3. **Deterministic statistics produce `Finding` objects; the model plans and
   narrates over findings only.** More engineering, permanently more correct.

## Decision

Option 3. Concretely:

- `services/evidence/` computes all statistics in code and may not import any
  model library (enforced by `.importlinter`).
- Every `Finding` row requires at least one `evidence_result_id`, enforced by a
  database `CHECK` constraint.
- The narrative agent has **no tool** that returns raw data — capability
  restriction, not prompt instruction.
- Generated prose is verified token-by-token against cited findings. Two
  failures degrade the response to evidence cards with no prose.

## Consequences

**Good.** Numeric fidelity becomes a testable 100% gate rather than a hope.
Cost per diagnostic stays around $0.17 because statistics are free. Far less
customer data crosses an LLM boundary, which is also a privacy win.

**Bad.** Every new analysis type requires real statistical work, not a prompt.
The evidence engine is the largest single component in the system.

**Accepted anyway** because this constraint *is* the moat: statistical
machinery does not commoditize when a new model ships.
