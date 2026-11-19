"""Retries with capped exponential backoff and full jitter."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from wfo.clock import Clock
from wfo.providers.base import ProviderError, RateLimitedError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.25
    max_delay_s: float = 8.0

    def backoff(self, attempt: int, rng: random.Random, retry_after: float | None = None) -> float:
        """Full-jitter delay for retry number ``attempt`` (0-based).

        Uniform in ``[0, min(max_delay, base * 2**attempt)]``, but never shorter than
        a vendor-provided ``Retry-After`` (itself capped at ``max_delay``).
        """
        cap = min(self.max_delay_s, self.base_delay_s * (2.0**attempt))
        delay = rng.uniform(0.0, cap)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_delay_s))
        return delay


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    clock: Clock,
    rng: random.Random,
    on_rate_limited: Callable[[], None] | None = None,
) -> tuple[T, int]:
    """Run ``fn`` until success or a non-retryable error; returns (result, attempts)."""
    attempt = 0
    while True:
        try:
            return await fn(), attempt + 1
        except ProviderError as exc:
            if not exc.retryable or attempt + 1 >= policy.max_attempts:
                raise
            retry_after = exc.retry_after if isinstance(exc, RateLimitedError) else None
            if isinstance(exc, RateLimitedError) and on_rate_limited is not None:
                on_rate_limited()
            await clock.sleep(policy.backoff(attempt, rng, retry_after))
            attempt += 1
