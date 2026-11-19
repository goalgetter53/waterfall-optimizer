from __future__ import annotations

import random
from collections.abc import Mapping

import pytest

from wfo.cache import InMemoryTTLCache
from wfo.clock import VirtualClock
from wfo.ledger import CostLedger
from wfo.models import Contact, Verification
from wfo.pipeline import EnrichmentPipeline, PipelineConfig, build_envelopes
from wfo.providers.base import Provider, ProviderError, ProviderResponse, RateLimitedError
from wfo.routing import CostModel, Router, StaticWaterfallRouter


def make_contact(
    i: int = 0, country: str = "US", employees: int = 30, seniority: str = "ic"
) -> Contact:
    return Contact(
        f"t{i}", "ada", f"lovelace{i}", f"acme{i}.example", country, employees, seniority
    )


class FakeProvider:
    """Scriptable provider for unit and fault-injection tests."""

    def __init__(
        self,
        name: str,
        cost: float = 0.02,
        email: str | None = "x@acme.example",
        charge_on_miss: bool = True,
        latency: float = 0.1,
        clock: VirtualClock | None = None,
    ) -> None:
        self.name = name
        self.cost_per_call = cost
        self.charge_on_miss = charge_on_miss
        self.email = email
        self.latency = latency
        self.clock = clock
        self.calls = 0
        self.fail_with: list[Exception] = []
        self.always_fail: Exception | None = None

    async def find_email(self, contact: Contact) -> ProviderResponse:
        self.calls += 1
        if self.clock is not None:
            await self.clock.sleep(self.latency)
        if self.always_fail is not None:
            raise self.always_fail
        if self.fail_with:
            raise self.fail_with.pop(0)
        if self.email is None:
            return ProviderResponse(None, self.cost_per_call if self.charge_on_miss else 0.0)
        return ProviderResponse(self.email, self.cost_per_call)


class FakeVerifier:
    def __init__(
        self,
        result: Verification = Verification.VALID,
        cost: float = 0.003,
        by_email: Mapping[str, Verification] | None = None,
    ) -> None:
        self.result = result
        self.cost_per_check = cost
        self.by_email = dict(by_email or {})
        self.checks = 0

    async def verify(
        self, contact: Contact, email: str, provider_status: str | None
    ) -> Verification:
        self.checks += 1
        return self.by_email.get(email, self.result)


def make_pipeline(
    providers: Mapping[str, Provider],
    clock: VirtualClock,
    router: Router | None = None,
    verifier: FakeVerifier | None = None,
    ledger: CostLedger | None = None,
    config: PipelineConfig | None = None,
    failure_threshold: int = 3,
) -> EnrichmentPipeline:
    costs = CostModel({n: p.cost_per_call for n, p in providers.items()}, 0.003)
    return EnrichmentPipeline(
        build_envelopes(
            providers,
            clock,
            rate_per_s=100.0,
            burst=100.0,
            failure_threshold=failure_threshold,
            reset_timeout_s=30.0,
        ),
        router or StaticWaterfallRouter(costs),
        verifier or FakeVerifier(),
        ledger or CostLedger(":memory:"),
        InMemoryTTLCache(clock.now),
        clock,
        config or PipelineConfig(),
        random.Random(0),
    )


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


__all__ = [
    "FakeProvider",
    "FakeVerifier",
    "ProviderError",
    "RateLimitedError",
    "make_contact",
    "make_pipeline",
]
