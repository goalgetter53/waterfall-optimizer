"""Circuit breaker: closed -> open -> half-open -> closed."""

from __future__ import annotations

from enum import StrEnum

from wfo.clock import Clock


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures.

    While open, calls fail fast (and the router skips the provider). After
    ``reset_timeout_s`` the breaker lets up to ``half_open_max_calls`` probes
    through; one success closes it, one failure re-opens it.
    """

    def __init__(
        self,
        clock: Clock,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.clock = clock
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.half_open_max_calls = half_open_max_calls
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self.transitions: list[tuple[BreakerState, BreakerState]] = []
        self.rejected = 0

    def _set(self, new: BreakerState) -> None:
        if new is not self._state:
            self.transitions.append((self._state, new))
            self._state = new

    @property
    def state(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self.clock.now() - self._opened_at >= self.reset_timeout_s
        ):
            self._set(BreakerState.HALF_OPEN)
            self._half_open_inflight = 0
        return self._state

    def available(self) -> bool:
        """Non-mutating check the router uses to filter candidates."""
        st = self.state
        if st is BreakerState.OPEN:
            return False
        if st is BreakerState.HALF_OPEN:
            return self._half_open_inflight < self.half_open_max_calls
        return True

    def before_call(self) -> None:
        st = self.state
        if st is BreakerState.OPEN:
            self.rejected += 1
            raise CircuitOpenError("circuit open")
        if st is BreakerState.HALF_OPEN:
            if self._half_open_inflight >= self.half_open_max_calls:
                self.rejected += 1
                raise CircuitOpenError("half-open probe already in flight")
            self._half_open_inflight += 1

    def on_success(self) -> None:
        self._failures = 0
        if self._state is BreakerState.HALF_OPEN:
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
        self._set(BreakerState.CLOSED)

    def on_failure(self) -> None:
        if self._state is BreakerState.HALF_OPEN:
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._opened_at = self.clock.now()
        self._failures = 0
        self._set(BreakerState.OPEN)
