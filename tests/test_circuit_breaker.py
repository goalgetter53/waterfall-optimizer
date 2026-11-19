from __future__ import annotations

import pytest

from wfo.clock import VirtualClock
from wfo.reliability import BreakerState, CircuitBreaker, CircuitOpenError


def test_opens_after_threshold_and_fails_fast(clock: VirtualClock) -> None:
    br = CircuitBreaker(clock, failure_threshold=3, reset_timeout_s=10)
    for _ in range(2):
        br.on_failure()
    assert br.state is BreakerState.CLOSED
    br.on_failure()
    assert br.state is BreakerState.OPEN
    assert not br.available()
    with pytest.raises(CircuitOpenError):
        br.before_call()
    assert br.rejected == 1


def test_success_resets_consecutive_failures(clock: VirtualClock) -> None:
    br = CircuitBreaker(clock, failure_threshold=3)
    br.on_failure()
    br.on_failure()
    br.on_success()
    br.on_failure()
    br.on_failure()
    assert br.state is BreakerState.CLOSED


def test_half_open_probe_success_closes(clock: VirtualClock) -> None:
    br = CircuitBreaker(clock, failure_threshold=1, reset_timeout_s=5)
    br.on_failure()
    clock.advance(5)
    assert br.state is BreakerState.HALF_OPEN
    br.before_call()  # the single probe
    assert not br.available()
    with pytest.raises(CircuitOpenError):
        br.before_call()
    br.on_success()
    assert br.state is BreakerState.CLOSED
    assert br.transitions == [
        (BreakerState.CLOSED, BreakerState.OPEN),
        (BreakerState.OPEN, BreakerState.HALF_OPEN),
        (BreakerState.HALF_OPEN, BreakerState.CLOSED),
    ]


def test_half_open_probe_failure_reopens(clock: VirtualClock) -> None:
    br = CircuitBreaker(clock, failure_threshold=1, reset_timeout_s=5)
    br.on_failure()
    clock.advance(6)
    br.before_call()
    br.on_failure()
    assert br.state is BreakerState.OPEN
    clock.advance(4.9)
    assert br.state is BreakerState.OPEN
    clock.advance(0.2)
    assert br.state is BreakerState.HALF_OPEN
