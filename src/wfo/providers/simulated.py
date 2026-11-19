"""Simulated providers with segment-specific hit rate, accuracy, cost, latency and errors.

All outcomes are deterministic functions of (world seed, contact, provider, call
number), so different routing strategies see *the same* world (common random
numbers), which makes per-contact regret comparisons meaningful.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from wfo.clock import Clock
from wfo.models import Contact
from wfo.providers.base import ProviderError, ProviderResponse, RateLimitedError
from wfo.sim.world import GUESS_FACTOR, SimProviderSpec, SyntheticWorld


class SimulatedProvider:
    def __init__(
        self,
        world: SyntheticWorld,
        spec: SimProviderSpec,
        clock: Clock,
        latency_scale: float = 1.0,
    ) -> None:
        self.world = world
        self.spec = spec
        self.name = spec.name
        self.cost_per_call = spec.cost_per_call
        self.charge_on_miss = spec.charge_on_miss
        self.clock = clock
        self.latency_scale = latency_scale
        self.calls = 0
        self._per_contact: dict[str, int] = defaultdict(int)
        # Fault injection knobs (used by tests and the fault scenario).
        self.outage = False
        self.rate_limit_storm = False
        self.outage_fn: Callable[[], bool] | None = None

    def _down(self) -> bool:
        return self.outage or (self.outage_fn is not None and self.outage_fn())

    async def find_email(self, contact: Contact) -> ProviderResponse:
        self.calls += 1
        n = self._per_contact[contact.id]
        self._per_contact[contact.id] = n + 1
        latency = self.world.latency(self.name, contact.id, n) * self.latency_scale

        if self._down():
            await self.clock.sleep(latency * 0.2)
            raise ProviderError(f"{self.name}: simulated outage (503)")
        if self.rate_limit_storm:
            await self.clock.sleep(latency * 0.1)
            raise RateLimitedError(f"{self.name}: simulated 429 storm", retry_after=0.5)
        u = self.world.uniform(contact.id, self.name, "err", n)
        if u < self.spec.error_rate:
            await self.clock.sleep(latency * 0.3)
            raise ProviderError(f"{self.name}: simulated 5xx")
        if u < self.spec.error_rate + self.spec.rate_limit_rate:
            await self.clock.sleep(latency * 0.1)
            raise RateLimitedError(f"{self.name}: simulated 429", retry_after=0.25)

        await self.clock.sleep(latency)
        params = self.world.params[contact.segment.key]
        truth = self.world.truth(contact)
        h = params.hit[self.name]
        u_hit = self.world.uniform(contact.id, self.name, "hit")
        if truth.discoverable:
            found = u_hit < h
            correct = self.world.uniform(contact.id, self.name, "acc") < params.accuracy[self.name]
        else:
            found = u_hit < GUESS_FACTOR * h
            correct = False
        if not found:
            charged = self.cost_per_call if self.charge_on_miss else 0.0
            return ProviderResponse(email=None, charged=charged)
        if correct:
            email = truth.true_email
        else:
            email = f"{contact.first_name[0]}{contact.last_name}@{contact.company_domain}".lower()
        return ProviderResponse(email=email, charged=self.cost_per_call)


def build_simulated_providers(
    world: SyntheticWorld, clock: Clock, latency_scale: float = 1.0
) -> dict[str, SimulatedProvider]:
    return {
        name: SimulatedProvider(world, spec, clock, latency_scale)
        for name, spec in world.specs.items()
    }
