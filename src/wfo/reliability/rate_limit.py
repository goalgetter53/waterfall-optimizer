"""Async token bucket, one per provider."""

from __future__ import annotations

import asyncio

from wfo.clock import Clock

_EPS = 1e-9


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, burst up to ``capacity``.

    ``acquire`` waits (on the injected clock) until a token is available, so the
    same code paces real HTTP calls and virtual-time simulations.
    """

    def __init__(self, rate: float, capacity: float, clock: Clock) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self.clock = clock
        self._tokens = capacity
        self._updated = clock.now()
        self._lock = asyncio.Lock()
        self.waited_s = 0.0

    def _refill(self) -> None:
        now = self.clock.now()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens + _EPS >= tokens:
            self._tokens = max(0.0, self._tokens - tokens)
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens``; returns seconds spent waiting."""
        if tokens > self.capacity:
            raise ValueError("cannot acquire more tokens than capacity")
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                # epsilon: float error after a computed wait must not cause a zero-length
                # re-wait loop (on a virtual clock t + 1e-17 == t)
                if self._tokens + _EPS >= tokens:
                    self._tokens = max(0.0, self._tokens - tokens)
                    self.waited_s += waited
                    return waited
                wait = (tokens - self._tokens) / self.rate
                waited += wait
                await self.clock.sleep(wait)

    def drain(self) -> None:
        """Empty the bucket (used when a vendor returns 429: back off globally)."""
        self._refill()
        self._tokens = 0.0
