from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wfo.clock import VirtualClock
from wfo.reliability import TokenBucket


async def test_burst_then_paced(clock: VirtualClock) -> None:
    bucket = TokenBucket(rate=2.0, capacity=2.0, clock=clock)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == 0.0
    waited = await bucket.acquire()
    assert waited == pytest.approx(0.5)
    assert clock.now() == pytest.approx(0.5)


def test_try_acquire_and_refill(clock: VirtualClock) -> None:
    bucket = TokenBucket(rate=1.0, capacity=1.0, clock=clock)
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    clock.advance(1.0)
    assert bucket.try_acquire()


def test_drain_forces_wait(clock: VirtualClock) -> None:
    bucket = TokenBucket(rate=4.0, capacity=4.0, clock=clock)
    bucket.drain()
    assert not bucket.try_acquire()


def test_rejects_invalid_config(clock: VirtualClock) -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(0, 1, clock)


@settings(max_examples=60, deadline=None)
@given(
    rate=st.floats(0.5, 50),
    capacity=st.integers(1, 20),
    gaps=st.lists(st.floats(0, 2), min_size=1, max_size=60),
)
def test_never_exceeds_rate_envelope(rate: float, capacity: int, gaps: list[float]) -> None:
    """Property: tokens granted <= capacity + rate * elapsed, for any arrival pattern."""

    async def scenario() -> None:
        clock = VirtualClock()
        bucket = TokenBucket(rate, capacity, clock)
        for granted, gap in enumerate(gaps, start=1):
            clock.advance(gap)
            await bucket.acquire()
            assert granted <= capacity + rate * clock.now() + 1e-6

    asyncio.run(scenario())
