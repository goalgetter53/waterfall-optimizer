"""asyncio worker pool over the durable lease queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from wfo.models import Contact
from wfo.pipeline import EnrichmentPipeline
from wfo.queue.base import JobQueue, Lease
from wfo.webhook import WebhookNotifier

log = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        queue: JobQueue,
        pipelines: Mapping[str, EnrichmentPipeline],
        concurrency: int = 8,
        visibility_timeout_s: float = 60.0,
        poll_interval_s: float = 0.05,
        webhook: WebhookNotifier | None = None,
        on_batch_complete: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.queue = queue
        self.pipelines = dict(pipelines)
        self.concurrency = concurrency
        self.visibility_timeout_s = visibility_timeout_s
        self.poll_interval_s = poll_interval_s
        self.webhook = webhook
        self.on_batch_complete = on_batch_complete
        self.processed = 0
        self.lost_leases = 0
        self.failures = 0

    async def run(self, stop: asyncio.Event) -> None:
        workers = [asyncio.create_task(self._loop(f"w{i}", stop)) for i in range(self.concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()

    async def run_until_drained(self, idle_polls: int = 3) -> None:
        """Process until the queue is empty (used by ``wfo run`` and tests)."""
        stop = asyncio.Event()
        task = asyncio.create_task(self.run(stop))
        idle = 0
        try:
            while idle < idle_polls:
                await asyncio.sleep(self.poll_interval_s)
                counts = self._pending()
                idle = idle + 1 if counts == 0 else 0
        finally:
            stop.set()
            await task

    def _pending(self) -> int:
        counts: Any = getattr(self.queue, "counts", lambda: {})()
        return int(counts.get("queued", 0)) + int(counts.get("leased", 0))

    async def _loop(self, worker_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            did_work = await self.process_one(worker_id)
            if not did_work:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)

    async def process_one(self, worker_id: str) -> bool:
        leases = self.queue.lease(worker_id, self.visibility_timeout_s, limit=1)
        if not leases:
            return False
        await self._process(leases[0])
        return True

    async def _heartbeat(self, lease: Lease) -> None:
        interval = self.visibility_timeout_s / 3
        while True:
            await asyncio.sleep(interval)
            if not self.queue.heartbeat(lease, self.visibility_timeout_s):
                log.warning("lost lease on job %s", lease.job_id)
                return

    async def _process(self, lease: Lease) -> None:
        payload = lease.payload
        contact = Contact.from_dict(payload["contact"])
        pipeline = self.pipelines[payload["strategy"]]
        hb = asyncio.create_task(self._heartbeat(lease))
        try:
            outcome = await pipeline.enrich(contact, job_key=f"{lease.batch_id}:{contact.id}")
        except Exception as exc:  # job-level failure: requeue with backoff, or dead-letter
            self.failures += 1
            log.exception("job %s failed", lease.job_id)
            self.queue.fail(lease, repr(exc), retry_delay_s=min(60.0, 2.0**lease.attempts))
        else:
            if self.queue.complete(lease, outcome.to_dict()):
                self.processed += 1
            else:
                self.lost_leases += 1  # another worker owns it now; our result is discarded
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        if self.queue.try_finalize_batch(lease.batch_id):
            await self._batch_completed(lease.batch_id)

    async def _batch_completed(self, batch_id: str) -> None:
        if self.on_batch_complete is not None:
            await self.on_batch_complete(batch_id)
        status = self.queue.batch_status(batch_id)
        if status is None or not status.webhook_url or self.webhook is None:
            return
        results = self.queue.results(batch_id)
        verified = sum(1 for r in results if r["result"] and r["result"]["verification"] == "valid")
        spend = sum(float(r["result"]["cost"]) for r in results if r["result"])
        payload = {
            "event": "batch.completed",
            "batch_id": batch_id,
            "strategy": status.strategy,
            "total": status.total,
            "counts": status.counts,
            "verified": verified,
            "spend_usd": round(spend, 6),
        }
        ok = await self.webhook.deliver(status.webhook_url, payload)
        self.queue.set_webhook_status(batch_id, "delivered" if ok else "failed")
