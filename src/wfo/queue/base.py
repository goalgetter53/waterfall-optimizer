"""Durable job queue interface (SQLite WAL by default, Postgres SKIP LOCKED optional).

Semantics: at-least-once delivery with leases. ``lease`` hands a job to one
worker together with a fencing ``token`` and an expiry. A worker that dies
simply stops heartbeating; once the lease expires the job is re-leased with a
new token, and any late ``complete``/``fail`` from the old worker is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class Lease:
    job_id: int
    batch_id: str
    token: str
    payload: dict[str, Any]
    attempts: int
    expires_at: float


@dataclass(slots=True)
class BatchStatus:
    batch_id: str
    status: str
    strategy: str
    total: int
    counts: dict[str, int] = field(default_factory=dict)
    webhook_url: str | None = None
    webhook_status: str | None = None
    created_at: float = 0.0
    completed_at: float | None = None

    @property
    def finished(self) -> int:
        return self.counts.get(JobStatus.DONE, 0) + self.counts.get(JobStatus.DEAD, 0)


class JobQueue(Protocol):
    def create_batch(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        strategy: str,
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        """Create a batch of jobs. Returns (batch_id, created). A repeated
        ``idempotency_key`` returns the existing batch with ``created=False``."""
        ...

    def lease(self, worker_id: str, visibility_timeout_s: float, limit: int = 1) -> list[Lease]: ...

    def heartbeat(self, lease: Lease, visibility_timeout_s: float) -> bool: ...

    def complete(self, lease: Lease, result: dict[str, Any]) -> bool: ...

    def fail(self, lease: Lease, error: str, retry_delay_s: float = 0.0) -> bool: ...

    def recover_expired(self) -> int: ...

    def try_finalize_batch(self, batch_id: str) -> bool: ...

    def set_webhook_status(self, batch_id: str, status: str) -> None: ...

    def batch_status(self, batch_id: str) -> BatchStatus | None: ...

    def results(self, batch_id: str) -> list[dict[str, Any]]: ...

    def batch_strategy(self, batch_id: str) -> str | None: ...
