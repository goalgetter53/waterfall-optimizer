"""Injectable clocks so the same runtime code runs in real time and in virtual time."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Monotonic wall-clock time with real ``asyncio.sleep``."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    """Discrete-event clock: ``sleep`` advances time instantly.

    Used by the simulation harness so tens of thousands of enrichments with
    realistic provider latency run in seconds while latency is still measured.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += max(0.0, seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
