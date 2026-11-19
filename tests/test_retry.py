from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from wfo.clock import VirtualClock
from wfo.providers.base import ProviderAuthError, ProviderError, RateLimitedError
from wfo.reliability import RetryPolicy, call_with_retry


@given(
    attempt=st.integers(0, 20),
    base=st.floats(0.01, 2),
    cap=st.floats(0.1, 30),
    retry_after=st.none() | st.floats(0, 100),
    seed=st.integers(0, 10_000),
)
def test_backoff_is_bounded_full_jitter(
    attempt: int, base: float, cap: float, retry_after: float | None, seed: int
) -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=base, max_delay_s=cap)
    d = policy.backoff(attempt, random.Random(seed), retry_after)
    upper = min(cap, base * 2**attempt)
    if retry_after is None:
        assert 0 <= d <= upper + 1e-9
    else:
        assert d >= min(retry_after, cap) - 1e-9
        assert d <= max(upper, min(retry_after, cap)) + 1e-9


async def test_retries_transient_then_succeeds(clock: VirtualClock) -> None:
    errors: list[Exception] = [ProviderError("503"), RateLimitedError(retry_after=1.0)]
    drained: list[int] = []

    async def fn() -> str:
        if errors:
            raise errors.pop(0)
        return "ok"

    result, attempts = await call_with_retry(
        fn,
        RetryPolicy(max_attempts=4),
        clock,
        random.Random(1),
        on_rate_limited=lambda: drained.append(1),
    )
    assert (result, attempts) == ("ok", 3)
    assert drained == [1]
    assert clock.now() >= 1.0  # honoured Retry-After


async def test_non_retryable_raises_immediately(clock: VirtualClock) -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise ProviderAuthError("401")

    with pytest.raises(ProviderAuthError):
        await call_with_retry(fn, RetryPolicy(max_attempts=5), clock, random.Random(0))
    assert calls == 1
    assert clock.now() == 0


async def test_gives_up_after_max_attempts(clock: VirtualClock) -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError("500")

    with pytest.raises(ProviderError):
        await call_with_retry(fn, RetryPolicy(max_attempts=3), clock, random.Random(0))
    assert calls == 3
