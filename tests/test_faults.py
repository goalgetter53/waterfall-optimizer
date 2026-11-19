"""Fault injection: provider outage, 429 storms, worker crash mid-job."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import FakeProvider, FakeVerifier, make_contact, make_pipeline
from wfo.cache import SqliteResponseCache
from wfo.clock import VirtualClock
from wfo.ledger import CostLedger
from wfo.models import StopReason
from wfo.pipeline import EnrichmentPipeline, build_envelopes
from wfo.providers.base import ProviderError, RateLimitedError
from wfo.queue import SqliteJobQueue
from wfo.reliability import BreakerState
from wfo.routing import CostModel, StaticWaterfallRouter
from wfo.worker import WorkerPool


async def test_provider_outage_opens_breaker_and_routes_around(clock: VirtualClock) -> None:
    down = FakeProvider("down", 0.01, clock=clock)
    down.always_fail = ProviderError("503 outage")
    backup = FakeProvider("backup", 0.05, clock=clock)
    pipe = make_pipeline({"down": down, "backup": backup}, clock, failure_threshold=3)
    outs = [await pipe.enrich(make_contact(i), f"k{i}") for i in range(30)]
    assert all(o.verified and o.provider == "backup" for o in outs)
    assert pipe.envelopes["down"].breaker.state is BreakerState.OPEN
    # 3 failures x up to 4 attempts would be 12; breaker caps the damage
    assert down.calls <= 3
    assert pipe.ledger.spent_by_provider().get("down", 0) == 0  # failures are free


async def test_breaker_recovers_through_half_open_probe(clock: VirtualClock) -> None:
    flaky = FakeProvider("flaky", 0.01, clock=clock)
    flaky.always_fail = ProviderError("503")
    other = FakeProvider("other", 0.05, clock=clock)
    pipe = make_pipeline({"flaky": flaky, "other": other}, clock, failure_threshold=2)
    await pipe.enrich(make_contact(0), "k0")
    breaker = pipe.envelopes["flaky"].breaker
    assert breaker.state is BreakerState.OPEN
    flaky.always_fail = None  # provider comes back
    clock.advance(31)
    out = await pipe.enrich(make_contact(1), "k1")
    assert out.provider == "flaky"
    assert breaker.state is BreakerState.CLOSED


async def test_429_storm_backs_off_and_falls_through(clock: VirtualClock) -> None:
    storm = FakeProvider("storm", 0.01, clock=clock)
    storm.always_fail = RateLimitedError(retry_after=2.0)
    calm = FakeProvider("calm", 0.05, clock=clock)
    pipe = make_pipeline({"storm": storm, "calm": calm}, clock, failure_threshold=50)
    out = await pipe.enrich(make_contact(), "k")
    assert out.verified
    assert out.provider == "calm"
    assert storm.calls == 4  # max_attempts
    assert out.calls[0].error is not None
    assert out.latency_s >= 3 * 2.0  # Retry-After honoured between attempts
    assert pipe.ledger.spent_by_provider() == {
        "calm": pytest.approx(0.05),
        "verifier": pytest.approx(0.003),
    }


async def test_transient_429_then_success_is_charged_once(clock: VirtualClock) -> None:
    p = FakeProvider("p", 0.02, clock=clock)
    p.fail_with = [RateLimitedError(retry_after=0.5), RateLimitedError(retry_after=0.5)]
    pipe = make_pipeline({"p": p}, clock)
    out = await pipe.enrich(make_contact(), "k")
    assert out.verified
    assert p.calls == 3
    assert pipe.ledger.spent_by_provider()["p"] == pytest.approx(0.02)


async def test_cancellation_mid_call_releases_budget_hold(clock: VirtualClock) -> None:
    class Hanging(FakeProvider):
        async def find_email(self, contact):  # type: ignore[no-untyped-def]
            self.calls += 1
            await asyncio.Event().wait()

    ledger = CostLedger(budget_usd=1.0)
    pipe = make_pipeline({"h": Hanging("h", 0.2)}, clock, ledger=ledger)
    task = asyncio.create_task(pipe.enrich(make_contact(), "k"))
    await asyncio.sleep(0.01)
    assert ledger.reserved_usd == pytest.approx(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.reserved_usd == 0


async def test_worker_crash_mid_job_is_recovered_without_double_charge(tmp_path: Path) -> None:
    """Worker A pays for the lookup, then dies before acking. After the visibility
    timeout worker B re-leases the job, gets the paid response from the shared cache,
    and completes it. A's late ack is fenced off; the ledger records one charge."""
    db = tmp_path / "wfo.db"
    now = [1_000.0]
    queue = SqliteJobQueue(db, now=lambda: now[0])
    clock = VirtualClock()
    provider = FakeProvider("hunter", 0.04, email="ada@acme0.example", clock=clock)
    ledger = CostLedger(db)
    cache = SqliteResponseCache(db, now=lambda: now[0])
    pipeline = EnrichmentPipeline(
        build_envelopes({"hunter": provider}, clock),
        StaticWaterfallRouter(CostModel({"hunter": 0.04}, 0.003)),
        FakeVerifier(),
        ledger,
        cache,
        clock,
    )
    contact = make_contact()
    batch, _ = queue.create_batch(
        [(contact.id, {"contact": contact.to_dict(), "strategy": "static"})], "static"
    )

    # Worker A: lease, do the paid work, crash before complete().
    (lease_a,) = queue.lease("A", visibility_timeout_s=30)
    await pipeline.enrich(contact, job_key=f"{batch}:{contact.id}")
    spent_after_a = ledger.spent_usd

    # Visibility timeout passes; worker B (a real WorkerPool) picks it up.
    now[0] += 31
    pool = WorkerPool(queue, {"static": pipeline}, concurrency=1)
    assert await pool.process_one("B")

    assert provider.calls == 1  # B was served from cache
    assert ledger.spent_usd == pytest.approx(spent_after_a) == pytest.approx(0.043)
    assert not queue.complete(lease_a, {"zombie": True})  # fenced
    (row,) = queue.results(batch)
    assert row["status"] == "done"
    assert row["result"]["verification"] == "valid"
    st = queue.batch_status(batch)
    assert st is not None
    assert st.status == "completed"
    assert pool.processed == 1


async def test_worker_exception_requeues_then_dead_letters(tmp_path: Path) -> None:
    now = [0.0]
    queue = SqliteJobQueue(tmp_path / "q.db", now=lambda: now[0], max_attempts=2)
    clock = VirtualClock()

    class Exploding:
        router = None

        async def enrich(self, contact, job_key):  # type: ignore[no-untyped-def]
            raise RuntimeError("bug in pipeline")

    contact = make_contact()
    batch, _ = queue.create_batch(
        [(contact.id, {"contact": contact.to_dict(), "strategy": "x"})], "x"
    )
    pool = WorkerPool(queue, {"x": Exploding()}, concurrency=1)  # type: ignore[dict-item]
    assert await pool.process_one("w")
    now[0] += 10
    assert await pool.process_one("w")
    assert queue.counts() == {"dead": 1}
    assert pool.failures == 2
    assert queue.try_finalize_batch(batch) is False  # already finalized by the worker
    del clock


async def test_all_providers_down_degrades_gracefully(clock: VirtualClock) -> None:
    provs = {n: FakeProvider(n, 0.02, clock=clock) for n in "abc"}
    for p in provs.values():
        p.always_fail = ProviderError("down")
    pipe = make_pipeline(provs, clock, failure_threshold=1)
    outs = [await pipe.enrich(make_contact(i), f"k{i}") for i in range(5)]
    assert all(o.stop_reason is StopReason.EXHAUSTED for o in outs)
    assert pipe.ledger.spent_usd == 0
