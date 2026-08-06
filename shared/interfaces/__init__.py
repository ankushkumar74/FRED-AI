"""Protocols that let Phase 1 implementations be swapped for Phase 3/4 ones.

Every call site depends on these, never on a concrete implementation.
See BLUEPRINT 4.7 — this module is why "build the interface now, the
implementation later" is achievable rather than aspirational.
"""
from typing import Protocol, Any, AsyncIterator


class JobQueue(Protocol):
    """[P1] arq/Redis  ->  [P3] Temporal Cloud"""
    async def enqueue(self, task: str, payload: dict[str, Any], *,
                      priority: int = 0, dedup_key: str | None = None) -> str: ...
    async def cancel(self, job_id: str) -> bool: ...


class EventBus(Protocol):
    """[P1] Postgres outbox -> Redis Streams  ->  [P4] Kafka/Redpanda"""
    async def publish(self, stream: str, event: dict[str, Any]) -> None: ...
    def subscribe(self, stream: str, group: str) -> AsyncIterator[dict[str, Any]]: ...


class VectorStore(Protocol):
    """[P1] pgvector  ->  [P4] dedicated store past ~50M vectors"""
    async def upsert(self, org_id: str, items: list[dict[str, Any]]) -> None: ...
    async def search(self, org_id: str, embedding: list[float], *,
                     k: int = 50, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str: ...
    async def get(self, key: str) -> bytes: ...


class LLMProvider(Protocol):
    async def complete(self, *, model: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None,
                       max_tokens: int = 4096) -> dict[str, Any]: ...
